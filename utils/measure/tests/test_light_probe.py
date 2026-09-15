from collections.abc import Iterable
from dataclasses import replace
from unittest.mock import MagicMock

from measure.assembler import MeasurementAssembler
from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightInfo
from measure.controller.light.spec import HassLightControllerSpec
from measure.ha_app.light_probe import (
    LightLoadProbe,
    LightLoadProbeError,
    app_measurement_assembler,
    light_load_probe_label,
)
from measure.powermeter.powermeter import PowerMeasurementResult
from measure.powermeter.spec import HassPowerMeterSpec
from measure.request import LightMeasurementRequest
from measure.runner.light_plan import (
    ColorTempVariation,
    HsVariation,
    LightMeasurementPlan,
    LightModePlan,
    Variation,
    low_load_probe_variations,
)
from measure.tuning import MeasurementParameters
from measure.util.measure_util import MeasureUtil
import pytest


class FakeLightController:
    def __init__(self, *, fail_cleanup: bool = False, events: list[tuple[str, object]] | None = None) -> None:
        self.changes: list[tuple[LutMode, bool, dict[str, object]]] = []
        self.closed = False
        self.fail_cleanup = fail_cleanup
        self.off_count = 0
        self.events = events

    def change_light_state(self, lut_mode: LutMode, on: bool = True, **kwargs: object) -> None:
        if not on:
            self.off_count += 1
            # The probe now turns off once to measure standby; cleanup turns off again.
            # Only the trailing cleanup off is allowed to fail this fixture.
            if self.fail_cleanup and self.off_count > 1:
                raise RuntimeError("turn-off failed")
        change = (lut_mode, on, kwargs)
        self.changes.append(change)
        if self.events is not None:
            self.events.append(("change", change))

    def get_light_info(self) -> LightInfo:
        return LightInfo("test", min_mired=153, max_mired=454)

    def has_effect_support(self) -> bool:
        return True

    def get_effect_list(self) -> list[str]:
        return ["colorloop"]

    def close(self) -> None:
        self.closed = True
        if self.fail_cleanup:
            raise RuntimeError("close failed")


class FakePowerMeter:
    def __init__(self, powers: Iterable[float], *, fail_close: bool = False) -> None:
        self._powers = iter(powers)
        self.calls = 0
        self.closed = False
        self.fail_close = fail_close

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        del include_voltage
        self.calls += 1
        return PowerMeasurementResult(next(self._powers), 100, None)

    def has_voltage_support(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("stuck")


class FakeAssembler:
    def __init__(self, controller: FakeLightController, meter: FakePowerMeter) -> None:
        self.controller = controller
        self.meter = meter

    def build_light_controller(self, _: object) -> FakeLightController:
        return self.controller

    def build_power_meter(self, _: object) -> FakePowerMeter:
        return self.meter


def request(
    *,
    parameters: MeasurementParameters | None = None,
    modes: set[LutMode] | None = None,
) -> LightMeasurementRequest:
    return LightMeasurementRequest(
        model_id="test",
        product_name="Test light",
        measure_device="Test meter",
        controller=HassLightControllerSpec(entity_id="light.test"),
        power_meter=HassPowerMeterSpec(entity_id="sensor.test_power"),
        modes=modes or {LutMode.HS},
        parameters=parameters or MeasurementParameters(sleep_time=0),
    )


def test_low_load_probe_variations_cover_static_mode_extremes_and_dedupe_hues() -> None:
    brightness = LightModePlan(LutMode.BRIGHTNESS, [Variation(5), Variation(1)])
    color_temp = LightModePlan(
        LutMode.COLOR_TEMP,
        [ColorTempVariation(1, 153), ColorTempVariation(1, 454), ColorTempVariation(10, 153)],
    )
    hs = LightModePlan(
        LutMode.HS,
        [HsVariation(1, 10000, 255), HsVariation(1, 30000, 255), HsVariation(1, 50000, 255)],
    )
    effects = LightModePlan(LutMode.EFFECT, [])

    assert low_load_probe_variations(LightMeasurementPlan([brightness, color_temp, hs, effects], [])) == [
        Variation(1),
        ColorTempVariation(1, 153),
        ColorTempVariation(1, 454),
        HsVariation(1, 10000, 255),
        HsVariation(1, 30000, 255),
        HsVariation(1, 50000, 255),
    ]


def test_active_probe_checks_rgb_primaries_and_caches_an_exact_request() -> None:
    controller = FakeLightController()
    meter = FakePowerMeter([0.2, 1.2, 0.9, 1.1])
    assembler = FakeAssembler(controller, meter)
    probe = LightLoadProbe(lambda: assembler, wait=lambda _: None, now=lambda: 10)

    result = probe.evaluate(request())
    cached = probe.evaluate(request())

    assert result == cached
    assert result.checked_variations == 3
    assert result.standby_aggregate_power_w == 0.2
    assert result.minimum_aggregate_power_w == 0.9
    assert [point.label for point in result.points] == [
        "Color 0° / 100% saturation · brightness 1",
        "Color 120° / 100% saturation · brightness 1",
        "Color 240° / 100% saturation · brightness 1",
    ]
    assert meter.calls == 4
    hues = [int(change[2]["hue"]) for change in controller.changes if change[0] == LutMode.HS and change[2]["bri"] == 1]
    assert [round(hue / 65535 * 360) for hue in hues] == [0, 120, 240]
    assert controller.changes[0] == (LutMode.BRIGHTNESS, False, {})
    assert controller.changes[-1] == (LutMode.BRIGHTNESS, False, {})
    assert controller.closed
    assert meter.closed


def test_active_probe_closes_the_power_meter_even_when_it_fails_to_close() -> None:
    """The active-light preflight check builds its own throwaway power meter (separate
    from the one the real measurement later builds); leaving it open leaks whatever
    resources it holds -- for OCR and composite meters, a bound preview-server port and
    a background capture thread -- so the very next attempt fails to bind that port."""
    controller = FakeLightController()
    meter = FakePowerMeter([0.2, 1.2, 0.9, 1.1], fail_close=True)
    assembler = FakeAssembler(controller, meter)
    probe = LightLoadProbe(lambda: assembler, wait=lambda _: None, now=lambda: 10)

    result = probe.evaluate(request())

    assert result.checked_variations == 3
    assert meter.closed
    assert controller.closed


@pytest.mark.parametrize(
    "mode, powers, expected_low",
    [
        (LutMode.BRIGHTNESS, [0.2, 1.2], (LutMode.BRIGHTNESS, True, {"bri": 1})),
        (LutMode.COLOR_TEMP, [0.2, 1.2, 1.1], (LutMode.COLOR_TEMP, True, {"bri": 1, "ct": 153})),
        (LutMode.HS, [0.2, 1.2, 0.9, 1.1], (LutMode.HS, True, {"bri": 1, "hue": 1, "sat": 255})),
    ],
)
def test_active_probe_skips_the_maximum_brightness_warmup(
    mode: LutMode,
    powers: list[float],
    expected_low: tuple[LutMode, bool, dict[str, int]],
) -> None:
    """Unlike the real run (runner/light.py), this probe never drives the light to
    maximum brightness first -- it only ever measures low-load points, so there's
    nothing here for that workaround (issue #2598, lights that turn off after rapid
    on/off commands) to protect against, and it would only add unwanted wait time.
    """
    controller = FakeLightController()
    meter = FakePowerMeter(powers)
    probe = LightLoadProbe(lambda: FakeAssembler(controller, meter), wait=lambda _: None, now=lambda: 10)

    probe.evaluate(request(modes={mode}))

    assert controller.changes[0] == (LutMode.BRIGHTNESS, False, {})
    assert controller.changes[1] == expected_low


def test_active_probe_uses_the_same_settle_and_standby_waits_as_a_lut_point() -> None:
    events: list[tuple[str, object]] = []
    controller = FakeLightController(events=events)
    meter = FakePowerMeter([0.2, 1.2, 0.9, 1.1])
    waits: list[float] = []
    parameters = MeasurementParameters(sleep_time=10, sleep_initial=8, sleep_standby=20)

    def record_wait(seconds: float) -> None:
        waits.append(seconds)
        events.append(("wait", seconds))

    probe = LightLoadProbe(
        lambda: FakeAssembler(controller, meter),
        wait=record_wait,
        now=lambda: 10,
    )

    probe.evaluate(request(parameters=parameters, modes={LutMode.HS}))

    # Standby: settle + sleep_standby. First on: settle + sleep_initial. Other ons: settle.
    assert waits == [10, 20, 10, 8, 10, 10]


def test_active_probe_uses_full_settle_detection_when_enabled() -> None:
    controller = FakeLightController()
    meter = FakePowerMeter([0.2, 1.2, 0.9, 1.1])
    parameters = MeasurementParameters(
        sleep_time=10,
        sleep_initial=0,
        sleep_standby=0,
        settle_tolerance_pct=5,
        settle_min_wait=0,
    )
    plateau_calls: list[tuple[float, float]] = []

    def spy(self: MeasureUtil, max_wait: float, *, tolerance_pct: float, **kwargs: object) -> float:
        del self, kwargs
        plateau_calls.append((max_wait, tolerance_pct))
        return 0.0

    probe = LightLoadProbe(lambda: FakeAssembler(controller, meter), wait=lambda _: None, now=lambda: 10)

    monkeypatch_target = MeasureUtil.wait_for_plateau
    MeasureUtil.wait_for_plateau = spy  # type: ignore[method-assign]
    try:
        probe.evaluate(request(parameters=parameters, modes={LutMode.HS}))
    finally:
        MeasureUtil.wait_for_plateau = monkeypatch_target  # type: ignore[method-assign]

    assert plateau_calls == [(10, 5.0)] * 4


def test_measure_step_records_one_standby_reading() -> None:
    controller = FakeLightController()
    meter = FakePowerMeter([0.2])
    probe = LightLoadProbe(lambda: FakeAssembler(controller, meter), wait=lambda _: None, now=lambda: 10)

    reading = probe.measure_step(request(parameters=MeasurementParameters(sleep_time=0, sleep_standby=0)), "standby")

    assert reading.kind == "standby"
    assert reading.power_w == 0.2
    assert controller.closed is True
    assert meter.closed is True


def test_active_probe_rejects_repeated_zero_on_saturated_green_and_cleans_up() -> None:
    controller = FakeLightController()
    meter = FakePowerMeter([0.1, 1.2, 0, 0, 0, 0, 0, 0])
    assembler = FakeAssembler(controller, meter)
    probe = LightLoadProbe(lambda: assembler, wait=lambda _: None, now=lambda: 10)

    with pytest.raises(LightLoadProbeError, match="repeatedly returned 0 W") as error:
        probe.evaluate(request())

    assert error.value.help_url == "https://docs.powercalc.nl/contributing/measure/low-power-measurements/"
    assert error.value.help_label == "Low-power measurement guide"
    assert controller.changes[-1] == (LutMode.BRIGHTNESS, False, {})
    assert controller.closed
    assert meter.calls == 8


def test_active_probe_uses_configured_sample_count_and_request_key() -> None:
    controller = FakeLightController()
    meter = FakePowerMeter([0.2, 0.2, 1, 2, 2, 3, 3, 4, 0.2, 0.2, 4, 5, 5, 6, 6, 7])
    assembler = FakeAssembler(controller, meter)
    probe = LightLoadProbe(lambda: assembler, wait=lambda _: None, now=lambda: 10)
    parameters = replace(MeasurementParameters(), sleep_time=0, sample_count=2, sleep_time_sample=0)

    first = probe.evaluate(request(parameters=parameters))
    second = probe.evaluate(request(parameters=replace(parameters, min_brightness=2)))

    assert first.standby_aggregate_power_w == 0.2
    assert first.minimum_aggregate_power_w == 1.5
    assert second.minimum_aggregate_power_w == 4.5
    assert meter.calls == 16


def test_active_probe_handles_effect_only_plan_and_formats_static_variations() -> None:
    controller = FakeLightController()
    assembler = FakeAssembler(controller, FakePowerMeter([]))
    probe = LightLoadProbe(lambda: assembler, wait=lambda _: None)

    result = probe.evaluate(request(modes={LutMode.EFFECT}))

    assert result.checked_variations == 0
    assert result.points == ()
    # Nothing was driven, so the light must be left exactly as the user had it.
    assert controller.changes == []
    assert controller.closed
    assert light_load_probe_label(Variation(1)) == "Brightness 1"
    assert light_load_probe_label(ColorTempVariation(1, 454)) == "Color temperature 2202 K · brightness 1"


def test_active_probe_wraps_controller_errors_and_cleanup_errors_do_not_mask_success() -> None:
    failing_assembler = MagicMock()
    failing_assembler.build_light_controller.side_effect = RuntimeError("controller unavailable")
    with pytest.raises(LightLoadProbeError, match="controller unavailable") as error:
        LightLoadProbe(lambda: failing_assembler).evaluate(request())

    # An adapter failure is not evidence of an unmeasurably low load.
    assert error.value.help_url is None

    controller = FakeLightController(fail_cleanup=True)
    meter = FakePowerMeter([0.2, 1.2, 0.9, 1.1])
    result = LightLoadProbe(
        lambda: FakeAssembler(controller, meter),
        wait=lambda _: None,
        now=lambda: 10,
    ).evaluate(request())

    assert result.checked_variations == 3
    assert controller.closed


def test_active_probe_rejects_an_on_load_that_matches_standby() -> None:
    controller = FakeLightController()
    meter = FakePowerMeter([0.68, 0.68, 0.70, 0.69])
    probe = LightLoadProbe(lambda: FakeAssembler(controller, meter), wait=lambda _: None, now=lambda: 10)

    with pytest.raises(LightLoadProbeError, match="too close to standby") as error:
        probe.evaluate(request())

    assert error.value.help_url == "https://docs.powercalc.nl/contributing/measure/low-power-measurements/"


def test_active_probe_uses_an_injected_power_meter_builder_instead_of_the_assembler() -> None:
    controller = FakeLightController()
    assembler_meter = FakePowerMeter([9.9])
    borrowed = FakePowerMeter([0.2, 1.2, 0.9, 1.1])
    assembler = FakeAssembler(controller, assembler_meter)

    LightLoadProbe(
        lambda: assembler,
        build_power_meter=lambda _spec: borrowed,
        wait=lambda _: None,
        now=lambda: 10,
    ).evaluate(request())

    assert assembler_meter.closed is False
    assert borrowed.closed is True


def test_app_measurement_assembler_builds_non_interactive_adapter_graph() -> None:
    assembler = app_measurement_assembler(home_assistant=MagicMock(), shelly_password="secret")  # noqa: S106

    assert isinstance(assembler, MeasurementAssembler)
