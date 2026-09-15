import csv
from dataclasses import dataclass, replace
import itertools
import os.path
from pathlib import Path
from unittest.mock import MagicMock

from measure.cli.questions import light_questions
from measure.controller.errors import ApiConnectionError as HassApiConnectionError
from measure.controller.errors import ControllerError
from measure.controller.light.const import LutMode
from measure.controller.light.dummy import DummyLightController
from measure.controller.light.spec import DummyLightControllerSpec
from measure.execution import RunInteraction
from measure.powermeter.errors import OutdatedMeasurementError, StandbyLikeOnError, ZeroReadingError
from measure.powermeter.spec import DummyPowerMeterSpec
from measure.request import LightMeasurementRequest, ResumePolicy
from measure.runner.const import QUESTION_MODE
from measure.runner.errors import RunnerError
from measure.runner.light import EffectVariation, LightRunner, MeasurementRunInput, _should_establish_on_off_bounds
from measure.runner.light_plan import (
    ColorTempVariation,
    HsVariation,
    LightMeasurementPlan,
    LightModePlan,
    Variation,
    build_light_plan,
    variations_after,
)
from measure.runner.on_off_bounds import OnOffBounds
from measure.runner.smart_envelope import MeasuredPoint, next_smart_batch, phase_a_variations
from measure.tuning import MeasurementParameters
from measure.util.measure_util import AverageMeasurementConvergence, MeasurementResult, MeasureUtil
import pytest


def _parameters() -> MeasurementParameters:
    return MeasurementParameters(
        ct_bri_steps=5,
        ct_mired_divisions=10,
        bri_bri_steps=1,
        hs_bri_steps=32,
        hs_hue_divisions=24,
        hs_sat_divisions=5,
    )


def _zero_sleep_parameters() -> MeasurementParameters:
    return replace(
        _parameters(),
        sleep_initial=0,
        sleep_time=0,
        sleep_time_ct=0,
        sleep_time_hue=0,
        sleep_time_sat=0,
        sleep_time_effect_change=0,
    )


def test_settle_waits_the_fixed_sleep_time_when_tolerance_is_zero() -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    config = replace(_parameters(), sleep_time=3, settle_tolerance_pct=0)
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner._settle()  # noqa: SLF001

    runner._wait.assert_called_once_with(3)  # noqa: SLF001
    measure_util_mock.wait_for_plateau.assert_not_called()


def test_dummy_benches_skip_on_off_bounds() -> None:
    request = LightMeasurementRequest(
        model_id="measurement",
        product_name="Measurement",
        measure_device="Test meter",
        power_meter=DummyPowerMeterSpec(),
        controller=DummyLightControllerSpec(),
        modes={LutMode.COLOR_TEMP},
        parameters=_zero_sleep_parameters(),
        gzip=False,
    )
    assert _should_establish_on_off_bounds(request) is False


def test_refine_uses_seed_lowest_on_instead_of_walking() -> None:
    measure_util = MagicMock(MeasureUtil)
    measure_util.take_measurement.return_value = MeasurementResult(power=0.74, voltages=[])
    runner = LightRunner(
        measure_util,
        replace(_zero_sleep_parameters(), sleep_standby=0),
        DummyLightController(),
    )
    runner.num_lights = 5
    runner.plan = LightMeasurementPlan(
        modes=[LightModePlan(mode=LutMode.COLOR_TEMP, variations=[ColorTempVariation(bri=255, ct=153)])],
        effects=[],
    )
    runner._seed_points_by_mode = {  # noqa: SLF001
        LutMode.COLOR_TEMP: [
            MeasuredPoint(ColorTempVariation(bri=177, ct=252), 0.14),
            MeasuredPoint(ColorTempVariation(bri=1, ct=476), 0.26),
        ],
    }
    runner._change_light_state_with_retry = MagicMock()  # noqa: SLF001
    runner.interaction = MagicMock()

    runner._establish_on_off_bounds()  # noqa: SLF001

    assert runner._on_off_bounds == OnOffBounds(standby=0.15, minimum_on=0.26)  # noqa: SLF001
    assert measure_util.take_measurement.call_count == 1
    measure_util.wait_for_plateau.assert_not_called()
    assert runner.measure_standby_power().power == 0.15
    assert measure_util.take_measurement.call_count == 1


def test_establish_reuses_recorded_standby_without_measuring(tmp_path: Path) -> None:
    from measure.runner.on_off_bounds import save_on_off_bounds

    save_on_off_bounds(tmp_path, OnOffBounds(standby=0.05, minimum_on=0.26))
    measure_util = MagicMock(MeasureUtil)
    runner = LightRunner(measure_util, _zero_sleep_parameters(), DummyLightController())
    runner._export_directory = str(tmp_path)  # noqa: SLF001
    runner._seed_points_by_mode = {  # noqa: SLF001
        LutMode.COLOR_TEMP: [MeasuredPoint(ColorTempVariation(bri=1, ct=476), 0.26)],
    }
    runner.interaction = MagicMock()

    runner._establish_on_off_bounds()  # noqa: SLF001

    assert runner._on_off_bounds == OnOffBounds(standby=0.05, minimum_on=0.26)  # noqa: SLF001
    measure_util.take_measurement.assert_not_called()


def test_establish_ignores_standby_inside_the_known_on_envelope() -> None:
    measure_util = MagicMock(MeasureUtil)
    measure_util.take_measurement.return_value = MeasurementResult(power=5.08, voltages=[])
    runner = LightRunner(
        measure_util,
        replace(_zero_sleep_parameters(), sleep_standby=0),
        DummyLightController(),
    )
    runner.num_lights = 5
    runner._seed_points_by_mode = {  # noqa: SLF001
        LutMode.HS: [MeasuredPoint(HsVariation(bri=1, hue=0, sat=255), 0.25 + index * 0.01) for index in range(20)],
    }
    runner._change_light_state_with_retry = MagicMock()  # noqa: SLF001
    runner.interaction = MagicMock()

    runner._establish_on_off_bounds()  # noqa: SLF001

    assert runner._on_off_bounds is None  # noqa: SLF001
    runner.interaction.notify.assert_called()  # noqa: SLF001


def test_repeated_standby_like_skips_the_point_without_aborting() -> None:
    runner = LightRunner(MagicMock(MeasureUtil), _zero_sleep_parameters(), DummyLightController())
    variation = ColorTempVariation(bri=1, ct=476)
    for _ in range(4):
        assert runner._skip_after_repeated_standby_like(variation) is False  # noqa: SLF001
    assert runner._skip_after_repeated_standby_like(variation) is True  # noqa: SLF001


def test_runner_rejects_an_on_sample_closer_to_standby_than_lowest_on() -> None:
    runner = LightRunner(MagicMock(MeasureUtil), _zero_sleep_parameters(), DummyLightController())
    runner._on_off_bounds = OnOffBounds(standby=0.14, minimum_on=0.30)  # noqa: SLF001
    with pytest.raises(StandbyLikeOnError, match="closer to standby"):
        runner._raise_if_standby_like_on(ColorTempVariation(bri=177, ct=252), 0.14)  # noqa: SLF001
    runner._raise_if_standby_like_on(ColorTempVariation(bri=177, ct=249), 1.33)  # noqa: SLF001


def test_settle_polls_for_a_plateau_when_tolerance_is_set() -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.wait_for_plateau.return_value = 1.2
    config = replace(
        _parameters(),
        sleep_time=10,
        settle_tolerance_pct=2.0,
        settle_min_wait=0,
        settle_window_seconds=1.5,
        settle_poll_interval_seconds=0.3,
    )
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner._settle()  # noqa: SLF001

    runner._wait.assert_not_called()  # noqa: SLF001
    measure_util_mock.wait_for_plateau.assert_called_once_with(
        10,
        tolerance_pct=2.0,
        tolerance_w=0.1,
        window_seconds=1.5,
        poll_interval=0.3,
        min_power=0.0,
    )
    assert runner.last_settle_seconds == 1.2
    assert runner.last_settle_hit_cap is False


def test_settle_records_none_for_settle_fields_when_tolerance_is_zero() -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    config = replace(_parameters(), sleep_time=3, settle_tolerance_pct=0)
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001
    runner.last_settle_seconds = 999.0  # left over from a previous variation
    runner.last_settle_hit_cap = True

    runner._settle()  # noqa: SLF001

    assert runner.last_settle_seconds is None
    assert runner.last_settle_hit_cap is None


def test_settle_with_tolerance_disabled_still_sets_a_current_log_context() -> None:
    """Plateau detection is opt-in; with it disabled there's no settle outcome to report,
    but the label must still be refreshed to something current -- otherwise it would show
    whatever a *previous* variation's settle wait left behind (e.g. "settle cap hit" from
    two variations ago), stale and misleading on a line that has nothing to do with it.
    """
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.power_meter = MagicMock(log_context="settle cap hit")  # stale, from a prior variation
    config = replace(_parameters(), sleep_time=3, settle_tolerance_pct=0)
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner._settle()  # noqa: SLF001

    assert measure_util_mock.power_meter.log_context == "settled"


def test_settle_flags_hitting_the_cap_when_the_plateau_wait_never_settled() -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.wait_for_plateau.return_value = 9.97  # never went flat, gave up near the 10s cap
    config = replace(_parameters(), sleep_time=10, settle_tolerance_pct=2.0, settle_min_wait=0)
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner._settle()  # noqa: SLF001

    assert runner.last_settle_hit_cap is True


def test_settle_labels_the_power_meters_log_context_when_it_actually_settled() -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.wait_for_plateau.return_value = 1.2
    measure_util_mock.power_meter = MagicMock(log_context="reading")
    config = replace(_parameters(), sleep_time=10, settle_tolerance_pct=2.0, settle_min_wait=0)
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner._settle()  # noqa: SLF001

    assert measure_util_mock.power_meter.log_context == "settled"


def test_settle_labels_the_power_meters_log_context_when_it_hit_the_cap_instead() -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.wait_for_plateau.return_value = 9.97  # never went flat, gave up near the 10s cap
    measure_util_mock.power_meter = MagicMock(log_context="reading")
    config = replace(_parameters(), sleep_time=10, settle_tolerance_pct=2.0, settle_min_wait=0)
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner._settle()  # noqa: SLF001

    assert measure_util_mock.power_meter.log_context == "settle cap hit"


def test_wait_skips_axis_wrap_sleeps_when_settle_is_on() -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.wait_for_plateau.return_value = 1.0
    config = replace(
        _parameters(),
        sleep_time=10,
        settle_tolerance_pct=5.0,
        settle_min_wait=0,
        sleep_time_ct=10,
        sleep_time_hue=5,
        sleep_time_sat=10,
    )
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner.wait(ColorTempVariation(bri=255, ct=153), ColorTempVariation(bri=255, ct=400))
    runner.wait(HsVariation(bri=255, hue=1, sat=64), HsVariation(bri=255, hue=40000, sat=255))
    runner.wait(HsVariation(bri=255, hue=1, sat=255), None)

    assert measure_util_mock.wait_for_plateau.call_count == 3
    runner._wait.assert_not_called()  # noqa: SLF001


def test_wait_still_adds_axis_wrap_sleeps_without_settle() -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    config = replace(_parameters(), sleep_time=2, settle_tolerance_pct=0, sleep_time_ct=10)
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner.wait(ColorTempVariation(bri=255, ct=153), ColorTempVariation(bri=255, ct=400))

    assert [call.args[0] for call in runner._wait.call_args_list] == [2, 10]  # noqa: SLF001


def test_settle_waits_before_looking_for_a_plateau() -> None:
    """A leftover flat reading (lamp still off / still at the last point) must not
    be accepted as the new plateau. The blind wait runs first so the LED can start
    moving; then climb fails flatness, or a new plateau is accepted.
    """
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.wait_for_plateau.return_value = 1.2
    config = replace(
        _parameters(),
        sleep_time=10,
        settle_tolerance_pct=2.0,
        settle_min_wait=2.0,
    )
    runner = LightRunner(measure_util_mock, config, DummyLightController())
    runner._wait = MagicMock()  # noqa: SLF001

    runner._settle()  # noqa: SLF001

    runner._wait.assert_called_once_with(2.0)  # noqa: SLF001
    measure_util_mock.wait_for_plateau.assert_called_once()
    assert runner.last_settle_seconds == pytest.approx(3.2)
    assert runner.last_settle_hit_cap is False


@dataclass
class _BrightnessRun:
    runner: LightRunner
    measurement_info: MeasurementRunInput
    all_variations: list[Variation]
    remaining_variations: list[Variation]
    measure_util: MagicMock

    def execute(self) -> None:
        self.runner.run_mode(self.measurement_info, self.all_variations, self.remaining_variations)


def _brightness_run(tmp_path: Path, variations: list[Variation]) -> _BrightnessRun:
    measure_util_mock = MagicMock(MeasureUtil)
    interaction = MagicMock(spec=RunInteraction)
    runner = LightRunner(measure_util_mock, _zero_sleep_parameters(), DummyLightController(), interaction)
    runner.gzip = False
    runner.light_info = runner.light_controller.get_light_info()
    runner.active_plan = LightMeasurementPlan(
        modes=[LightModePlan(mode=LutMode.BRIGHTNESS, variations=variations)],
        effects=[],
    )
    all_variations = variations.copy()
    remaining_variations = variations.copy()
    measurement_info = MeasurementRunInput(
        mode=LutMode.BRIGHTNESS,
        csv_file=str(tmp_path / "brightness.csv"),
        variations=variations.copy(),
        is_resuming=False,
    )
    return _BrightnessRun(runner, measurement_info, all_variations, remaining_variations, measure_util_mock)


@pytest.mark.parametrize(
    "mode,expected_count",
    [
        (
            LutMode.BRIGHTNESS,
            255,
        ),
        (
            LutMode.COLOR_TEMP,
            520,
        ),
        (
            LutMode.HS,
            1080,
        ),
        (
            LutMode.EFFECT,
            24,
        ),
    ],
)
def test_get_variations(mode: LutMode, expected_count: int) -> None:
    controller = DummyLightController()
    plan = build_light_plan(
        {mode},
        _parameters(),
        controller.get_light_info(),
        controller.get_effect_list(),
    )

    assert plan.variation_count == expected_count


@pytest.mark.parametrize(
    "mode,expected_count",
    [
        (LutMode.BRIGHTNESS, 2),
        (LutMode.COLOR_TEMP, 4),
        (LutMode.HS, 8),
        (LutMode.EFFECT, 6),
    ],
)
def test_fast_test_mode_uses_only_dimension_endpoints(mode: LutMode, expected_count: int) -> None:
    controller = DummyLightController()
    plan = build_light_plan(
        {mode},
        replace(_parameters(), fast_test_mode=True),
        controller.get_light_info(),
        controller.get_effect_list(),
    )

    assert plan.variation_count == expected_count


def test_smart_plan_is_phase_a_only() -> None:
    controller = DummyLightController()
    plan = build_light_plan(
        {LutMode.COLOR_TEMP},
        replace(_parameters(), smart_sampling=True),
        controller.get_light_info(),
    )
    assert plan.variation_count == len(plan.modes[0].variations)
    assert plan.variation_count > 4
    assert all(point.bri == 255 for point in plan.modes[0].variations)


def test_smart_run_appends_coverage_in_the_same_session(tmp_path: Path) -> None:
    last: dict[str, object] = {}

    class TrackingController(DummyLightController):
        def change_light_state(self, lut_mode: LutMode, on: bool = True, **kwargs: object) -> None:
            last.clear()
            last.update(kwargs)

    def measure(*_args: object, **_kwargs: object) -> MeasurementResult:
        bri = int(last.get("bri") or 255)
        ct = int(last.get("ct") or 150)
        peak = 2.0 + 4.0 * (ct - 150) / 350
        return MeasurementResult(power=peak * (bri / 255), voltages=[])

    measure_util = MagicMock(MeasureUtil)
    measure_util.take_measurement.side_effect = measure
    config = replace(
        _zero_sleep_parameters(),
        smart_sampling=True,
        smart_delta=8.0,
        smart_border_delta=40.0,
    )
    runner = LightRunner(measure_util, config, TrackingController(), MagicMock(spec=RunInteraction))
    request = LightMeasurementRequest(
        model_id="measurement",
        product_name="Measurement",
        measure_device="Test meter",
        power_meter=DummyPowerMeterSpec(),
        controller=DummyLightControllerSpec(),
        modes={LutMode.COLOR_TEMP},
        parameters=config,
        gzip=False,
    )
    runner.run(request, str(tmp_path))
    rows = list(csv.DictReader((tmp_path / "color_temp.csv").open(encoding="utf-8")))
    assert len(rows) > 6
    stages = [call.args[0] for call in runner.interaction.phase.call_args_list]
    assert "Discovering envelope" in stages
    assert "Covering interior" in stages


def _smart_ct_prefix(config: MeasurementParameters) -> tuple[list[MeasuredPoint], list[ColorTempVariation]]:
    """Phase-A plus half of the first invented mid-bri batch — the 564e2858 failure shape."""

    light = DummyLightController().get_light_info()
    measured = [
        MeasuredPoint(
            variation,
            (5.0 if isinstance(variation, ColorTempVariation) and variation.ct < 250 else 2.5) * (variation.bri / 255),
        )
        for variation in phase_a_variations(LutMode.COLOR_TEMP, config, light)
    ]
    for _ in range(16):
        batch = next_smart_batch(LutMode.COLOR_TEMP, measured, config, light)
        if not batch.variations:
            break
        invented = [point for point in batch.variations if point.bri != config.max_brightness]
        if len(invented) > 2:
            take = max(1, len(batch.variations) // 2)
            prefix = [MeasuredPoint(point, 3.0 * (point.bri / 255)) for point in batch.variations[:take]]
            remaining = [point for point in batch.variations[take:] if isinstance(point, ColorTempVariation)]
            return [*measured, *prefix], remaining
        measured.extend(MeasuredPoint(point, 3.0 * (point.bri / 255)) for point in batch.variations)
    raise AssertionError("smart planner never left the 100% sweep")


def _write_ct_csv(path: Path, points: list[MeasuredPoint]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["bri", "mired", "watt"])
        for point in points:
            assert isinstance(point.variation, ColorTempVariation)
            writer.writerow([point.variation.bri, point.variation.ct, f"{point.watt:.2f}"])


def test_smart_resume_replays_past_invented_rows(tmp_path: Path) -> None:
    config = replace(_zero_sleep_parameters(), smart_sampling=True, smart_delta=30.0)
    measured, remaining = _smart_ct_prefix(config)
    csv_path = tmp_path / "color_temp.csv"
    _write_ct_csv(csv_path, measured)
    last = measured[-1].variation
    assert isinstance(last, ColorTempVariation)
    assert last.bri != 255
    with pytest.raises(RunnerError, match="does not match the configured measurement grid"):
        variations_after(phase_a_variations(LutMode.COLOR_TEMP, config, DummyLightController().get_light_info()), last)

    runner = LightRunner(MagicMock(MeasureUtil), config, DummyLightController())
    runner._configure(  # noqa: SLF001
        LightMeasurementRequest(
            model_id="measurement",
            product_name="Measurement",
            measure_device="Test meter",
            power_meter=DummyPowerMeterSpec(),
            controller=DummyLightControllerSpec(),
            modes={LutMode.COLOR_TEMP},
            resume_policy=ResumePolicy.RESUME,
            parameters=config,
            gzip=False,
        ),
    )
    prepared = runner.prepare_measurements_for_mode(str(tmp_path), LutMode.COLOR_TEMP)

    assert prepared.is_resuming is True
    assert last not in prepared.variations
    assert prepared.variations == remaining
    assert len(prepared.seed_rows) == len(measured)


def test_smart_resume_keeps_existing_rows_and_continues(tmp_path: Path) -> None:
    last: dict[str, object] = {}

    class TrackingController(DummyLightController):
        def change_light_state(self, lut_mode: LutMode, on: bool = True, **kwargs: object) -> None:
            last.clear()
            last.update(kwargs)

    def measure(*_args: object, **_kwargs: object) -> MeasurementResult:
        bri = int(last.get("bri") or 255)
        return MeasurementResult(power=round(4.0 * (bri / 255), 2), voltages=[])

    config = replace(_zero_sleep_parameters(), smart_sampling=True, smart_delta=30.0)
    measured, _remaining = _smart_ct_prefix(config)
    _write_ct_csv(tmp_path / "color_temp.csv", measured)
    original = list(csv.DictReader((tmp_path / "color_temp.csv").open(encoding="utf-8")))

    measure_util = MagicMock(MeasureUtil)
    measure_util.take_measurement.side_effect = measure
    runner = LightRunner(measure_util, config, TrackingController(), MagicMock(spec=RunInteraction))
    runner.run(
        LightMeasurementRequest(
            model_id="measurement",
            product_name="Measurement",
            measure_device="Test meter",
            power_meter=DummyPowerMeterSpec(),
            controller=DummyLightControllerSpec(),
            modes={LutMode.COLOR_TEMP},
            resume_policy=ResumePolicy.RESUME,
            parameters=config,
            gzip=False,
        ),
        str(tmp_path),
    )
    rows = list(csv.DictReader((tmp_path / "color_temp.csv").open(encoding="utf-8")))
    assert rows[: len(original)] == original
    assert len(rows) > len(original)


def test_run(export_path: str) -> None:
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.take_measurement.return_value = MeasurementResult(power=1, voltages=[])
    interaction = MagicMock(spec=RunInteraction)
    runner = LightRunner(measure_util_mock, _parameters(), DummyLightController(), interaction)
    request = LightMeasurementRequest(
        model_id="measurement",
        product_name="Measurement",
        measure_device="Test meter",
        power_meter=DummyPowerMeterSpec(),
        controller=DummyLightControllerSpec(),
        modes={LutMode.BRIGHTNESS},
    )
    result = runner.run(request, export_path)
    assert result.model_json_data == {
        "device_type": "light",
        "calculation_strategy": "lut",
    }

    assert os.path.exists(os.path.join(export_path, "brightness.csv.gz"))
    remaining = [call.kwargs["remaining_seconds"] for call in interaction.progress.call_args_list]
    assert remaining[0] > remaining[-1]
    assert remaining[-1] == 0
    interaction.phase.assert_any_call(
        "Stabilizing light before the first reading",
        wait_seconds=10,
    )
    points = [call.args[0] for call in interaction.operating_point.call_args_list]
    assert points[0] == {"type": "light", "on": True, "brightness": 255}
    assert points[1] == {"type": "light", "on": True, "brightness": 1}
    assert points[-1] == {"type": "light", "on": True, "brightness": 254}


@pytest.mark.parametrize("completed_brightnesses", [[1], [1, 128]])
def test_resume_reports_progress_against_the_full_plan(
    tmp_path: Path,
    completed_brightnesses: list[int],
) -> None:
    parameters = replace(_zero_sleep_parameters(), bri_bri_steps=127)
    measure_util = MagicMock(MeasureUtil)
    measure_util.take_measurement.return_value = MeasurementResult(power=1, voltages=[])
    interaction = MagicMock(spec=RunInteraction)
    runner = LightRunner(
        measure_util,
        parameters,
        DummyLightController(),
        interaction,
        resume=True,
    )
    request = LightMeasurementRequest(
        model_id="measurement",
        product_name="Measurement",
        measure_device="Test meter",
        power_meter=DummyPowerMeterSpec(),
        controller=DummyLightControllerSpec(),
        modes={LutMode.BRIGHTNESS},
        parameters=parameters,
        gzip=False,
    )
    csv_path = tmp_path / "brightness.csv"
    with csv_path.open("w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["bri", "watt"])
        writer.writerows([brightness, 1.0] for brightness in completed_brightnesses)

    runner.run(request, str(tmp_path))

    initial_progress = interaction.progress.call_args_list[0].kwargs
    assert initial_progress["completed"] == len(completed_brightnesses)
    assert initial_progress["total"] == 3
    final_progress = interaction.progress.call_args_list[-1].kwargs
    assert final_progress["completed"] == 3
    assert final_progress["total"] == 3
    with csv_path.open(newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    assert [int(row[0]) for row in rows[1:]] == [1, 128, 255]


def test_initial_wait_happens_after_selecting_first_measurement_point(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    variation = Variation(1)
    run = _brightness_run(tmp_path, [variation])
    run.runner.config = replace(run.runner.config, sleep_time=2, sleep_initial=10)
    light_controller = MagicMock(spec=DummyLightController)
    light_controller.change_light_state.side_effect = lambda *args, **kwargs: events.append(("change", (args, kwargs)))
    run.runner.light_controller = light_controller
    run.runner.interaction.wait.side_effect = lambda seconds: events.append(("wait", seconds))
    run.measure_util.take_measurement.return_value = MeasurementResult(power=1, voltages=[])

    run.execute()

    assert events[:5] == [
        ("change", ((LutMode.BRIGHTNESS,), {"on": True, "bri": 255})),
        ("change", ((LutMode.BRIGHTNESS,), {"on": True, "bri": 255})),
        ("change", ((LutMode.BRIGHTNESS,), {"on": True, "bri": 1})),
        ("wait", 2),
        ("wait", 10),
    ]
    run.runner.interaction.phase.assert_any_call(
        "Stabilizing light before the first reading",
        wait_seconds=10,
    )
    run.runner.interaction.phase.assert_any_call(
        "Full-brightness warm-up (1 of 2): sending command",
    )


def test_zero_reading_retries_current_variation_and_reports_skipped_progress(tmp_path: Path) -> None:
    variations = [Variation(1), Variation(2)]
    run = _brightness_run(tmp_path, variations)
    run.measure_util.take_measurement.side_effect = [
        ZeroReadingError("0 watt was read from the power meter"),
        MeasurementResult(power=1, voltages=[]),
        MeasurementResult(power=2, voltages=[]),
    ]

    run.execute()

    with open(run.measurement_info.csv_file, newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    assert rows == [["bri", "watt"], ["1", "1.0"], ["2", "2.0"]]
    skipped_progress = run.runner.interaction.progress.call_args_list[1]
    assert skipped_progress.kwargs["completed"] == 0
    assert skipped_progress.kwargs["total"] == 2
    assert skipped_progress.kwargs["skipped"] == 1
    assert run.runner.interaction.progress.call_args_list[-1].kwargs["completed"] == 2
    assert run.runner.interaction.progress.call_args_list[-1].kwargs["total"] == 2


def test_zero_reading_counter_resets_after_valid_measurement(tmp_path: Path) -> None:
    variations = [Variation(1), Variation(2)]
    run = _brightness_run(tmp_path, variations)
    run.measure_util.take_measurement.side_effect = [
        ZeroReadingError("first low reading"),
        MeasurementResult(power=1, voltages=[]),
        ZeroReadingError("second low reading"),
        ZeroReadingError("third low reading"),
        ZeroReadingError("fourth low reading"),
        ZeroReadingError("fifth low reading"),
        MeasurementResult(power=2, voltages=[]),
    ]

    run.execute()

    with open(run.measurement_info.csv_file, newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    assert rows[-2:] == [["1", "1.0"], ["2", "2.0"]]


def test_outdated_measurement_retries_the_point_when_nudging_is_off(tmp_path: Path) -> None:
    variations = [Variation(1)]
    run = _brightness_run(tmp_path, variations)
    run.measure_util.take_measurement.side_effect = [
        OutdatedMeasurementError(
            "OCR: last accepted reading is 5.5s old; last frame: power unreadable: ''",
        ),
        MeasurementResult(power=4.2, voltages=[]),
    ]

    run.execute()

    with open(run.measurement_info.csv_file, newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    assert rows == [["bri", "watt"], ["1", "4.2"]]
    assert run.measure_util.take_measurement.call_count == 2


def test_repeated_zero_readings_fail_fast_with_actionable_error(tmp_path: Path) -> None:
    variations = [Variation(1), Variation(2)]
    run = _brightness_run(tmp_path, variations)
    run.measure_util.take_measurement.side_effect = ZeroReadingError("0 watt was read from the power meter")

    with pytest.raises(RunnerError) as error:
        run.execute()

    message = str(error.value)
    assert "repeated 0 W readings" in message
    assert "power meter may not resolve this low load" in message
    assert "multiple identical lights" in message
    assert "resistive dummy load" in message
    assert "https://docs.powercalc.nl/contributing/measure/troubleshooting/" in message
    assert run.measure_util.take_measurement.call_count == 5
    assert run.runner.interaction.progress.call_args_list[-1].kwargs["skipped"] == 5


def test_cleanup_turns_off_light() -> None:
    light_controller = MagicMock(spec=DummyLightController)
    runner = LightRunner(MagicMock(MeasureUtil), _parameters(), light_controller)

    runner.cleanup()

    light_controller.change_light_state.assert_called_once_with(LutMode.BRIGHTNESS, on=False)
    light_controller.close.assert_called_once_with()


def test_cleanup_failure_does_not_mask_measurement_result(caplog: pytest.LogCaptureFixture) -> None:
    light_controller = MagicMock(spec=DummyLightController)
    light_controller.change_light_state.side_effect = RuntimeError("unavailable")
    runner = LightRunner(MagicMock(MeasureUtil), _parameters(), light_controller)

    runner.cleanup()

    assert "Could not turn off MagicMock during measurement cleanup: unavailable" in caplog.text
    light_controller.close.assert_called_once_with()


def test_controller_close_failure_does_not_mask_measurement_result(caplog: pytest.LogCaptureFixture) -> None:
    light_controller = MagicMock(spec=DummyLightController)
    light_controller.close.side_effect = RuntimeError("close unavailable")
    runner = LightRunner(MagicMock(MeasureUtil), _parameters(), light_controller)

    runner.cleanup()

    assert "Could not close the light controller during measurement cleanup: close unavailable" in caplog.text


def _flaky_light_controller(failures_after_startup: int, *, start_failing_at: int = 2) -> MagicMock:
    """A light controller that drops its connection after ``start_failing_at`` calls.

    The first two calls belong to set_light_to_maximum_brightness, which runs before
    any variation is measured; ``start_failing_at=0`` targets that initial turn-on.
    """

    light_controller = MagicMock(spec=DummyLightController)
    calls = itertools.count()

    def change_light_state(*_: object, **__: object) -> None:
        index = next(calls)
        if index >= start_failing_at and index - start_failing_at < failures_after_startup:
            raise HassApiConnectionError("Failed to change light state: Connection broken")

    light_controller.change_light_state.side_effect = change_light_state
    return light_controller


def test_run_mode_writes_one_raw_sample_per_variation_for_a_bare_power_meter(tmp_path: Path) -> None:
    """A bare meter still records each accepted reading so merge quality can count them."""
    import json

    variations = [Variation(1), Variation(2)]
    run = _brightness_run(tmp_path, variations)
    run.measure_util.take_measurement.side_effect = [
        MeasurementResult(power=1, voltages=[]),
        MeasurementResult(power=2, voltages=[]),
    ]

    run.execute()

    raw_path = tmp_path / "brightness.raw.jsonl"
    lines = [json.loads(line) for line in raw_path.read_text().splitlines()]
    assert [line["variation"] for line in lines] == [{"bri": 1}, {"bri": 2}]
    assert [line["primary"]["power"] for line in lines] == [1, 2]


def test_run_mode_records_one_raw_sample_per_variation_from_the_composite_meters_last_reading(
    tmp_path: Path,
) -> None:
    import json

    from measure.powermeter.composite import CompositePowerMeter, CompositeReading, WitnessReading
    from measure.powermeter.powermeter import PowerMeasurementResult

    variations = [Variation(1), Variation(2)]
    run = _brightness_run(tmp_path, variations)
    readings = [
        CompositeReading(
            primary=PowerMeasurementResult(power=1.0, updated=1.0, voltage=230.0, current=0.01, power_factor=0.4),
            witnesses=(
                WitnessReading(
                    name="ocr",
                    power=0.9,
                    corrected=0.9,
                    deviation=-0.1,
                    agrees=True,
                    voltage=230.1,
                    current=0.009,
                    power_factor=0.41,
                ),
            ),
        ),
        CompositeReading(
            primary=PowerMeasurementResult(power=2.0, updated=2.0),
            witnesses=(
                WitnessReading(name="ocr", power=None, corrected=None, deviation=None, agrees=True, error="stale"),
            ),
        ),
    ]
    composite = MagicMock(spec=CompositePowerMeter)
    run.measure_util.power_meter = composite

    # Simulates the composite meter's last_reading updating on every get_power() the
    # runner triggers internally via take_measurement().
    call_count = {"n": 0}

    def _take_measurement(*_args: object, **_kwargs: object) -> MeasurementResult:
        composite.last_reading = readings[call_count["n"]]
        call_count["n"] += 1
        return MeasurementResult(power=composite.last_reading.primary.power, voltages=[])

    run.measure_util.take_measurement.side_effect = _take_measurement

    run.execute()

    raw_path = tmp_path / "brightness.raw.jsonl"
    lines = [json.loads(line) for line in raw_path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["mode"] == "brightness"
    assert lines[0]["variation"] == {"bri": 1}
    assert lines[0]["primary"] == {"power": 1.0, "voltage": 230.0, "current": 0.01, "power_factor": 0.4}
    assert lines[0]["witnesses"] == [
        {
            "name": "ocr",
            "power": 0.9,
            "corrected": 0.9,
            "deviation": -0.1,
            "agrees": True,
            "error": None,
            "voltage": 230.1,
            "current": 0.009,
            "power_factor": 0.41,
        },
    ]
    assert lines[1]["variation"] == {"bri": 2}
    assert lines[1]["witnesses"][0]["error"] == "stale"
    assert lines[1]["witnesses"][0]["power"] is None
    # settle detection wasn't enabled for this run (_zero_sleep_parameters leaves
    # settle_tolerance_pct at its default 0), so both points record no settle data.
    assert lines[0]["settle_seconds"] is None
    assert lines[0]["settle_hit_cap"] is None


def test_warmup_retries_a_dropped_connection(tmp_path: Path) -> None:
    """#2598 warm-up used to wrap ApiConnectionError as a fatal RunnerError immediately."""

    variations = [Variation(1)]
    run = _brightness_run(tmp_path, variations)
    light_controller = MagicMock(spec=DummyLightController)
    calls = itertools.count()

    def change_light_state(*_: object, **__: object) -> None:
        if next(calls) == 0:
            raise HassApiConnectionError("Connection broken")

    light_controller.change_light_state.side_effect = change_light_state
    run.runner.light_controller = light_controller
    run.measure_util.take_measurement.side_effect = [MeasurementResult(power=1, voltages=[])]

    run.execute()

    with open(run.measurement_info.csv_file, newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    assert rows == [["bri", "watt"], ["1", "1.0"]]
    assert light_controller.change_light_state.call_count == 4


def test_change_light_state_is_retried_after_a_dropped_connection(tmp_path: Path) -> None:
    """A Home Assistant light must survive a dropped WebSocket mid-session.

    HassLightController raises measure.controller.errors.ApiConnectionError, while the
    runner used to catch a same-named class from measure.controller.light.errors. The
    two were unrelated, so the retry never fired and hours-long sessions died on a
    single broken pipe. See issue #4543.
    """

    variations = [Variation(1), Variation(2)]
    run = _brightness_run(tmp_path, variations)
    run.runner.light_controller = _flaky_light_controller(failures_after_startup=1)
    run.measure_util.take_measurement.side_effect = [
        MeasurementResult(power=1, voltages=[]),
        MeasurementResult(power=2, voltages=[]),
    ]

    run.execute()

    with open(run.measurement_info.csv_file, newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    assert rows == [["bri", "watt"], ["1", "1.0"], ["2", "2.0"]]


def test_change_light_state_keeps_retrying_until_home_assistant_accepts(tmp_path: Path) -> None:
    variations = [Variation(1)]
    run = _brightness_run(tmp_path, variations)
    run.runner.light_controller = _flaky_light_controller(failures_after_startup=5)
    run.measure_util.take_measurement.side_effect = [MeasurementResult(power=1, voltages=[])]

    run.execute()

    with open(run.measurement_info.csv_file, newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    assert rows == [["bri", "watt"], ["1", "1.0"]]
    assert run.runner.light_controller.change_light_state.call_count == 8
    retry_waits = [
        call.args[0] for call in run.runner.interaction.wait.call_args_list if call.args and call.args[0] == 15.0
    ]
    assert retry_waits == [15.0] * 5


def test_stuck_light_ack_is_retried_until_the_light_reports(tmp_path: Path) -> None:
    variations = [Variation(1)]
    run = _brightness_run(tmp_path, variations)
    light_controller = MagicMock(spec=DummyLightController)
    calls = itertools.count()

    def change_light_state(*_: object, **__: object) -> None:
        index = next(calls)
        if 2 <= index < 5:
            raise ControllerError(
                "Lights did not reach on after 20.0s: light.slow. "
                "Measurement stopped so a stuck light cannot poison the LUT.",
            )

    light_controller.change_light_state.side_effect = change_light_state
    run.runner.light_controller = light_controller
    run.measure_util.take_measurement.side_effect = [MeasurementResult(power=1, voltages=[])]

    run.execute()

    assert light_controller.change_light_state.call_count == 6
    assert [
        call.args[0] for call in run.runner.interaction.wait.call_args_list if call.args and call.args[0] == 15.0
    ] == [15.0] * 3


def test_initial_maximum_brightness_is_retried_after_a_dropped_connection(tmp_path: Path) -> None:
    """A dropped connection during the initial turn-on must not abort the session.

    The retry only covered the per-variation loop, so a single broken frame during
    set_light_to_maximum_brightness still killed the run before any measurement was
    written.
    """

    variations = [Variation(1), Variation(2)]
    run = _brightness_run(tmp_path, variations)
    run.runner.light_controller = _flaky_light_controller(failures_after_startup=1, start_failing_at=0)
    run.measure_util.take_measurement.side_effect = [
        MeasurementResult(power=1, voltages=[]),
        MeasurementResult(power=2, voltages=[]),
    ]

    run.execute()

    with open(run.measurement_info.csv_file, newline="") as csv_file:
        rows = list(csv.reader(csv_file))
    assert rows == [["bri", "watt"], ["1", "1.0"], ["2", "2.0"]]


def test_initial_maximum_brightness_gives_up_after_five_failed_retries(tmp_path: Path) -> None:
    run = _brightness_run(tmp_path, [Variation(1)])
    run.runner.light_controller = _flaky_light_controller(failures_after_startup=5, start_failing_at=0)

    with pytest.raises(RunnerError, match="Failed to change light state after 5 retries"):
        run.execute()


def test_resume_effect(tmp_path: Path) -> None:
    """Test resume point is detected correctly for effect mode."""
    csv_file = tmp_path / "effect.csv"
    with open(csv_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["effect", "bri", "watt"])
        writer.writerow(["colorloop", 100, 2.5])
        writer.writerow(["nightlight", 200, 3.0])

    measure_util_mock = MagicMock(MeasureUtil)
    runner = LightRunner(measure_util_mock, _parameters(), DummyLightController())

    resume_variation = runner.get_resume_variation(str(csv_file), LutMode.EFFECT)
    assert isinstance(resume_variation, EffectVariation)
    assert resume_variation.effect == "nightlight"
    assert resume_variation.bri == 200


def test_resume_confirmation_uses_interaction(tmp_path: Path) -> None:
    csv_file = tmp_path / "brightness.csv"
    csv_file.write_text("bri,watt\n1,1.0\n")
    interaction = MagicMock(spec=RunInteraction)
    interaction.choose.return_value = False
    runner = LightRunner(
        MagicMock(MeasureUtil),
        replace(_parameters(), prompt_resume=True),
        DummyLightController(),
        interaction=interaction,
        resume=True,
    )

    assert runner.should_resume(str(csv_file)) is False
    interaction.choose.assert_called_once_with(
        f"CSV File {csv_file} already exists. Do you want to resume measurements?",
        default=True,
    )


def test_effect_measurement_uses_convergence_settings() -> None:
    parameters = replace(
        _parameters(),
        measure_time_effect=180,
        measure_time_effect_min=20,
        measure_time_effect_convergence_window=15,
        measure_time_effect_convergence_abs=0.1,
        measure_time_effect_convergence_rel=0.01,
    )
    measure_util_mock = MagicMock(MeasureUtil)
    measure_util_mock.take_average_measurement.return_value = MeasurementResult(power=10, voltages=[])
    runner = LightRunner(measure_util_mock, parameters, DummyLightController())

    runner.take_power_measurement(LutMode.EFFECT, start_timestamp=0)

    measure_util_mock.take_average_measurement.assert_called_once_with(
        180,
        convergence=AverageMeasurementConvergence(
            min_duration=20,
            window_duration=15,
            absolute_threshold=0.1,
            relative_threshold=0.01,
        ),
    )


def test_extend_skips_keys_already_in_the_seed_csv(tmp_path: Path) -> None:
    csv_file = tmp_path / "brightness.csv"
    csv_file.write_text("bri,watt\n1,1.0\n", encoding="utf-8")
    runner = LightRunner(MagicMock(MeasureUtil), _zero_sleep_parameters(), DummyLightController())
    runner._configure(  # noqa: SLF001
        LightMeasurementRequest(
            model_id="measurement",
            product_name="Measurement",
            measure_device="Test meter",
            power_meter=DummyPowerMeterSpec(),
            controller=DummyLightControllerSpec(),
            modes={LutMode.BRIGHTNESS},
            resume_policy=ResumePolicy.EXTEND,
            seed_session_id="seed-session",
            parameters=_zero_sleep_parameters(),
        ),
    )

    prepared = runner.prepare_measurements_for_mode(str(tmp_path), LutMode.BRIGHTNESS)

    assert Variation(1) not in prepared.variations
    assert Variation(255) in prepared.variations
    assert prepared.is_resuming is True


def test_extend_remeasure_runs_the_full_plan(tmp_path: Path) -> None:
    csv_file = tmp_path / "brightness.csv"
    csv_file.write_text("bri,watt\n1,1.0\n", encoding="utf-8")
    runner = LightRunner(MagicMock(MeasureUtil), _zero_sleep_parameters(), DummyLightController())
    runner._configure(  # noqa: SLF001
        LightMeasurementRequest(
            model_id="measurement",
            product_name="Measurement",
            measure_device="Test meter",
            power_meter=DummyPowerMeterSpec(),
            controller=DummyLightControllerSpec(),
            modes={LutMode.BRIGHTNESS},
            resume_policy=ResumePolicy.EXTEND,
            seed_session_id="seed-session",
            remeasure_existing=True,
            parameters=_zero_sleep_parameters(),
        ),
    )

    prepared = runner.prepare_measurements_for_mode(str(tmp_path), LutMode.BRIGHTNESS)

    assert Variation(1) in prepared.variations
    assert prepared.is_resuming is False
    assert prepared.seed_rows[Variation(1)].watt == 1.0


def test_new_run_premeasures_emitter_min_and_max_brightness(tmp_path: Path) -> None:
    runner = LightRunner(MagicMock(MeasureUtil), _zero_sleep_parameters(), DummyLightController())
    runner._configure(  # noqa: SLF001
        LightMeasurementRequest(
            model_id="measurement",
            product_name="Measurement",
            measure_device="Test meter",
            power_meter=DummyPowerMeterSpec(),
            controller=DummyLightControllerSpec(),
            modes={LutMode.BRIGHTNESS},
            parameters=_zero_sleep_parameters(),
        ),
    )

    prepared = runner.prepare_measurements_for_mode(str(tmp_path), LutMode.BRIGHTNESS)

    assert [variation.bri for variation in prepared.variations[:3]] == [255, 1, 2]
    assert prepared.variations[-1] == Variation(254)
    assert prepared.is_resuming is False


def test_resume_does_not_premeasure_the_opposite_bound(tmp_path: Path) -> None:
    csv_file = tmp_path / "brightness.csv"
    csv_file.write_text("bri,watt\n50,2.0\n", encoding="utf-8")
    runner = LightRunner(MagicMock(MeasureUtil), _zero_sleep_parameters(), DummyLightController())
    runner._configure(  # noqa: SLF001
        LightMeasurementRequest(
            model_id="measurement",
            product_name="Measurement",
            measure_device="Test meter",
            power_meter=DummyPowerMeterSpec(),
            controller=DummyLightControllerSpec(),
            modes={LutMode.BRIGHTNESS},
            resume_policy=ResumePolicy.RESUME,
            parameters=_zero_sleep_parameters(),
        ),
    )

    prepared = runner.prepare_measurements_for_mode(str(tmp_path), LutMode.BRIGHTNESS)

    assert prepared.is_resuming is True
    assert prepared.variations[0] == Variation(51)
    assert Variation(255) not in prepared.variations[:1]


def test_get_questions() -> None:
    """Test get_questions contains the new triple mode choice when effects are supported."""
    measure_util_mock = MagicMock(MeasureUtil)
    runner = LightRunner(measure_util_mock, _parameters(), DummyLightController())

    questions = light_questions(supports_effects=runner.light_controller.has_effect_support())
    mode_question = next(q for q in questions if q.name == QUESTION_MODE)
    choices = mode_question.choices

    assert ("hs + color_temp + effect", {LutMode.HS, LutMode.COLOR_TEMP, LutMode.EFFECT}) in choices
