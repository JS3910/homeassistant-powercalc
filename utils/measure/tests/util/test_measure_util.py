import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from measure.const import Trend
from measure.execution import MeasurementCancelledError
from measure.powermeter.errors import (
    ApiConnectionError,
    OutdatedMeasurementError,
    UnsupportedFeatureError,
    WaitingForMeterError,
    WitnessDisagreementError,
)
from measure.powermeter.powermeter import PowerMeasurementResult, PowerMeter
from measure.util.measure_util import (
    AverageMeasurementState,
    DummyLoadMeasurementError,
    MeasurementResult,
    MeasureUtil,
    NoValidReadingsError,
    _should_reconnect_meter,
)
import pytest

from tests.conftest import MockConfigFactory


@pytest.mark.parametrize(
    "values, expected",
    [
        ([1.0, 2.0, 3.0], 1.0),
        ([3.0, 2.0, 1.0], -1.0),
        ([4.0], 0.0),
    ],
)
def test_linear_slope_does_not_require_numpy(values: list[float], expected: float) -> None:
    assert MeasureUtil._linear_slope(values) == pytest.approx(expected)  # noqa: SLF001


@pytest.mark.parametrize(
    "averages, expected",
    [
        # Sub-threshold drift on a high-ohm load (0.5 Ω/sample on ~6.2 kΩ) is meter noise, not a trend
        ([6226.0 + 0.5 * index for index in range(20)], Trend.STEADY),
        # Real warm-up drift still registers regardless of resistance magnitude
        ([6000.0 + 10.0 * index for index in range(20)], Trend.INCREASING),
        ([6000.0 - 10.0 * index for index in range(20)], Trend.DECREASING),
        # The relative threshold keeps its sensitivity on low-ohm loads such as incandescent bulbs
        ([40.0 + 0.05 * index for index in range(20)], Trend.INCREASING),
        # Drift in only one half still means the load has not settled
        ([6000.0 + 10.0 * index for index in range(10)] + [6100.0] * 10, Trend.INCREASING),
        ([6100.0] * 10 + [6100.0 - 10.0 * index for index in range(10)], Trend.DECREASING),
    ],
)
def test_dummy_load_trend_uses_relative_threshold(averages: list[float], expected: Trend) -> None:
    assert MeasureUtil.dummy_load_trend(averages) == expected


def test_dummy_load_trend_requires_twenty_samples() -> None:
    assert MeasureUtil.dummy_load_trend([6226.0] * 19) is None


def test_dummy_load_trend_rejects_opposing_drift_as_unstable() -> None:
    averages = [6000.0 + 10.0 * index for index in range(10)] + [6100.0 - 10.0 * index for index in range(10)]

    assert MeasureUtil.dummy_load_trend(averages) is Trend.UNSTABLE


def test_no_valid_average_readings_raise_typed_error(mock_config_factory: MockConfigFactory) -> None:
    measure_util = MeasureUtil(MagicMock(PowerMeter), mock_config_factory())
    empty = AverageMeasurementState(start_time=0, readings=[], snapshots=[], voltages=[])

    with (
        patch.object(measure_util, "_collect_average_measurements", return_value=empty),
        pytest.raises(NoValidReadingsError),
    ):
        measure_util.take_average_measurement(1)


@pytest.mark.parametrize("interrupt", [MeasurementCancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("finish_on_interrupt", [False, True])
def test_average_stop_preserves_samples_only_when_requested(
    mock_config_factory: MockConfigFactory,
    interrupt: type[BaseException],
    finish_on_interrupt: bool,
) -> None:
    clock = [0.0]
    meter = MagicMock(PowerMeter)
    meter.get_power.side_effect = [
        PowerMeasurementResult(power=4.0, voltage=230.0, updated=0),
        PowerMeasurementResult(power=8.0, voltage=232.0, updated=0),
    ]

    def wait(seconds: float) -> None:
        clock[0] += seconds
        if meter.get_power.call_count == 2:
            raise interrupt

    progress = MagicMock()
    util = MeasureUtil(meter, mock_config_factory({"sleep_time": 2}), include_voltage=lambda: True, wait=wait)
    with patch("time.time", side_effect=lambda: clock[0]):
        if not finish_on_interrupt:
            with pytest.raises(interrupt):
                util.take_average_measurement(60, on_progress=progress)
        else:
            result = util.take_average_measurement(60, on_progress=progress, finish_on_interrupt=True)
            assert result == MeasurementResult(power=6.0, voltages=[230.0, 232.0])
            progress.assert_called_with(4.0, 60)


@pytest.mark.parametrize("interrupt", [MeasurementCancelledError, KeyboardInterrupt])
def test_average_stop_without_readings_is_not_successful(
    mock_config_factory: MockConfigFactory,
    interrupt: type[BaseException],
) -> None:
    meter = MagicMock(PowerMeter)
    meter.get_power.side_effect = interrupt
    util = MeasureUtil(meter, mock_config_factory())
    with pytest.raises(interrupt):
        util.take_average_measurement(60, finish_on_interrupt=True)


def test_take_measurement_keeps_the_settle_outcome_label_instead_of_overwriting_it(
    mock_config_factory: MockConfigFactory,
) -> None:
    """LightRunner._settle() labels the meter "settled" or "settle cap hit" right before
    this call so the eventual "Meters: ..." log line says *why* the sample is being
    accepted, not just that it is. Unconditionally overwriting that with a generic
    "accepted" label (the old behavior) threw the settle outcome away before it was ever
    logged -- this is exactly what left "Accepted" in the log instead of "Stable"/"Reached
    max settle time" despite LightRunner already setting the right label.
    """
    power_meter = MagicMock()
    power_meter.log_context = "settled"
    power_meter.get_power.return_value = PowerMeasurementResult(power=1.0, voltage=None, updated=time.time())
    measure_util = MeasureUtil(power_meter, mock_config_factory())

    measure_util.take_measurement()

    assert power_meter.log_context == "settled"


def test_take_measurement_emits_each_accepted_sample_before_averaging(
    mock_config_factory: MockConfigFactory,
) -> None:
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.side_effect = [
        PowerMeasurementResult(power=1.0, voltage=None, updated=time.time()),
        PowerMeasurementResult(power=3.0, voltage=None, updated=time.time()),
    ]
    accepted: list[float] = []
    measure_util = MeasureUtil(
        power_meter,
        mock_config_factory({"sample_count": 2, "sleep_time_sample": 0}),
        wait=lambda _: None,
        on_accepted_reading=lambda result: accepted.append(result.power),
    )

    result = measure_util.take_measurement()

    assert accepted == [1.0, 3.0]
    assert result.power == pytest.approx(2.0)


def test_take_measurement_still_labels_accepted_samples_with_no_prior_settle_outcome(
    mock_config_factory: MockConfigFactory,
) -> None:
    """Runners with no settle step of their own (charging/fan/recorder/speaker, the
    light-load preflight probe) never set "settled"/"settle cap hit", so their accepted
    samples keep the previous generic labeling unchanged.
    """
    power_meter = MagicMock()
    power_meter.log_context = "reading"
    power_meter.get_power.return_value = PowerMeasurementResult(power=1.0, voltage=None, updated=time.time())
    measure_util = MeasureUtil(power_meter, mock_config_factory())

    measure_util.take_measurement()

    assert power_meter.log_context == "accepted"


def test_dummy_load_requires_voltage_support(mock_config_factory: MockConfigFactory) -> None:
    power_meter = MagicMock(PowerMeter)
    power_meter.has_voltage_support.return_value = False
    measure_util = MeasureUtil(power_meter, mock_config_factory())

    with pytest.raises(UnsupportedFeatureError):
        measure_util.set_dummy_load_resistance(42.5)


def test_resistance_reading_emits_live_calibration_values(mock_config_factory: MockConfigFactory) -> None:
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.return_value = PowerMeasurementResult(power=60.0, voltage=230.0, updated=time.time())
    samples: list[tuple[float, float, float]] = []
    measure_util = MeasureUtil(
        power_meter,
        mock_config_factory(),
        on_calibration_sample=lambda power, resistance, voltage: samples.append((power, resistance, voltage)),
    )

    result = measure_util._take_resistance_reading()  # noqa: SLF001

    assert result == MeasurementResult(power=pytest.approx(881.6667), voltages=[230.0])
    assert samples == [(60.0, pytest.approx(881.6667), 230.0)]


def test_dummy_load_emits_corrected_sample(mock_config_factory: MockConfigFactory) -> None:
    power_meter = MagicMock(PowerMeter)
    power_meter.has_voltage_support.return_value = True
    power_meter.get_power.return_value = PowerMeasurementResult(power=20.0, voltage=10.0, updated=time.time())
    samples: list[float] = []
    measure_util = MeasureUtil(power_meter, mock_config_factory(), on_sample=samples.append)
    measure_util.set_dummy_load_resistance(10.0)

    result = measure_util.take_measurement()

    assert result.power == pytest.approx(10.0)
    assert samples
    assert all(sample == pytest.approx(10.0) for sample in samples)


@patch("time.time")
def test_average_measurement_uses_dummy_load_correction_pipeline(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    power_meter = MagicMock(PowerMeter)
    power_meter.has_voltage_support.return_value = True
    power_meter.get_power.return_value = PowerMeasurementResult(power=20.0, voltage=10.0, updated=0.0)
    samples: list[float] = []
    measure_util = MeasureUtil(power_meter, mock_config_factory(), on_sample=samples.append)
    measure_util.set_dummy_load_resistance(10.0)
    mock_time.side_effect = lambda: 100.0 if power_meter.get_power.call_count else 0.0

    result = measure_util.take_average_measurement(duration=10)

    assert result == MeasurementResult(power=10.0, voltages=[10.0])
    assert samples == [10.0]
    power_meter.get_power.assert_called_once_with(include_voltage=True)


def test_measurement_retries_outdated_reading_without_emitting_it(mock_config_factory: MockConfigFactory) -> None:
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.side_effect = [
        PowerMeasurementResult(power=5.0, updated=10.0),
        PowerMeasurementResult(power=7.0, updated=30.0),
    ]
    samples: list[float] = []
    measure_util = MeasureUtil(
        power_meter,
        mock_config_factory({"max_retries": 1, "sample_count": 1}),
        wait=lambda _: None,
        on_sample=samples.append,
    )

    result = measure_util.take_measurement(start_timestamp=20.0)

    assert result == MeasurementResult(power=7.0, voltages=[])
    assert samples == [7.0]
    assert power_meter.get_power.call_count == 2
    power_meter.recover.assert_not_called()


def test_stale_reading_before_the_point_started_waits_for_a_fresh_one(
    mock_config_factory: MockConfigFactory,
) -> None:
    """A reading from before this point can clear itself. max_retries must not abort."""
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.side_effect = [
        PowerMeasurementResult(power=5.0, updated=10.0),
        PowerMeasurementResult(power=5.0, updated=10.0),
        PowerMeasurementResult(power=7.0, updated=30.0),
    ]
    measure_util = MeasureUtil(
        power_meter,
        mock_config_factory({"max_retries": 0, "sample_count": 1}),
        wait=lambda _: None,
    )

    result = measure_util.take_measurement(start_timestamp=20.0)

    assert result == MeasurementResult(power=7.0, voltages=[])
    assert power_meter.get_power.call_count == 3
    power_meter.recover.assert_not_called()


def test_dummy_load_rejects_non_positive_corrected_power(mock_config_factory: MockConfigFactory) -> None:
    power_meter = MagicMock(PowerMeter)
    power_meter.has_voltage_support.return_value = True
    power_meter.get_power.return_value = PowerMeasurementResult(power=10.0, voltage=10.0, updated=time.time())
    measure_util = MeasureUtil(power_meter, mock_config_factory())
    measure_util.set_dummy_load_resistance(10.0)

    with pytest.raises(DummyLoadMeasurementError, match="non-positive target power"):
        measure_util.take_measurement()


class _ErrorThenSuccessPowerMeter(PowerMeter):
    """Power meter that raises errors for the first N calls, then succeeds."""

    def __init__(self, error_count: int, success_power: float = 5.0) -> None:
        self._error_count = error_count
        self._success_power = success_power
        self._call_count = 0
        self.recover_count = 0

    def recover(self) -> None:
        self.recover_count += 1

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        self._call_count += 1
        if self._call_count <= self._error_count:
            raise ApiConnectionError(f"Connection timeout (call {self._call_count})")
        return PowerMeasurementResult(power=self._success_power, updated=time.time())

    def has_voltage_support(self) -> bool:
        return False

    def process_answers(self, answers: dict[str, Any]) -> None:
        """No-op: not needed for test power meters."""

    @property
    def call_count(self) -> int:
        return self._call_count


class _AlwaysFailPowerMeter(PowerMeter):
    """Power meter that always raises ApiConnectionError."""

    def __init__(self) -> None:
        self._call_count = 0
        self.recover_count = 0

    def recover(self) -> None:
        self.recover_count += 1

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        self._call_count += 1
        raise ApiConnectionError(f"Connection timeout (call {self._call_count})")

    def has_voltage_support(self) -> bool:
        return False

    def process_answers(self, answers: dict[str, Any]) -> None:
        """No-op: not needed for test power meters."""

    @property
    def call_count(self) -> int:
        return self._call_count


@patch("time.time")
def test_average_measurement_retries_on_transient_error(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single transient error should be retried and the measurement should complete."""
    caplog.set_level(logging.WARNING)
    mock_config = mock_config_factory(config_values={"max_retries": 3})
    power_meter = _ErrorThenSuccessPowerMeter(error_count=1, success_power=5.0)
    measure_util = MeasureUtil(power_meter, mock_config)

    mock_time.side_effect = lambda: 100.0 if power_meter.call_count > 1 else 0.0

    result = measure_util.take_average_measurement(duration=10)

    assert result.power > 0
    assert power_meter.call_count == 2
    assert power_meter.recover_count == 1
    assert "Error during average measurement (attempt 1/3)" in caplog.text


@patch("time.time")
def test_average_measurement_retries_multiple_consecutive_errors(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """Multiple consecutive errors within max_retries should be tolerated."""
    mock_config = mock_config_factory(config_values={"max_retries": 3})
    power_meter = _ErrorThenSuccessPowerMeter(error_count=3, success_power=4.0)
    measure_util = MeasureUtil(power_meter, mock_config)

    mock_time.side_effect = lambda: 100.0 if power_meter.call_count > 3 else 0.0

    result = measure_util.take_average_measurement(duration=10)

    assert result.power == 4.0
    assert power_meter.call_count == 4


@patch("time.time", return_value=0.0)
def test_average_measurement_raises_after_max_retries_exceeded(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """Consecutive errors exceeding max_retries should re-raise the error."""
    mock_config = mock_config_factory(config_values={"max_retries": 2})
    power_meter = _AlwaysFailPowerMeter()
    measure_util = MeasureUtil(power_meter, mock_config)

    with pytest.raises(ApiConnectionError):
        measure_util.take_average_measurement(duration=10)

    # Should have been called max_retries + 1 times (initial + retries)
    assert power_meter.call_count == 3
    assert power_meter.recover_count == 2


@patch("time.time")
def test_average_measurement_resets_error_count_on_success(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """After a successful reading, the consecutive error counter should reset."""
    mock_config = mock_config_factory(config_values={"max_retries": 2})

    call_count = 0

    class _IntermittentPowerMeter(PowerMeter):
        """Fails once, succeeds once, fails once, succeeds — never exceeds max_retries consecutively."""

        def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
            nonlocal call_count
            call_count += 1
            # Fail on calls 1 and 3, succeed on calls 2, 4, 5, ...
            if call_count in (1, 3):
                raise ApiConnectionError(f"Timeout (call {call_count})")
            return PowerMeasurementResult(power=3.0, updated=time.time())

        def has_voltage_support(self) -> bool:
            return False

        def process_answers(self, answers: dict[str, Any]) -> None:
            """No-op: not needed for test power meters."""

    power_meter = _IntermittentPowerMeter()
    measure_util = MeasureUtil(power_meter, mock_config)

    mock_time.side_effect = lambda: 100.0 if call_count >= 4 else 0.0

    result = measure_util.take_average_measurement(duration=10)

    assert result.power == 3.0
    assert call_count == 4


@patch("time.time")
def test_average_measurement_excludes_failed_readings_from_average(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """The average should only include successful readings, not be affected by errors."""
    mock_config = mock_config_factory(config_values={"max_retries": 3})
    # First call fails, subsequent calls return exactly 7.0
    power_meter = _ErrorThenSuccessPowerMeter(error_count=1, success_power=7.0)
    measure_util = MeasureUtil(power_meter, mock_config)

    mock_time.side_effect = lambda: 100.0 if power_meter.call_count >= 3 else 0.0

    result = measure_util.take_average_measurement(duration=10)

    # Average should be exactly 7.0 since all successful readings are 7.0
    assert result.power == 7.0


@pytest.mark.parametrize(
    ("values", "tolerance_pct", "tolerance_w", "expected"),
    [
        # One Shelly LSB at ~1.45 W is ~7%, so a 5% band alone never succeeds.
        ([1.40, 1.50, 1.40], 5.0, 0.0, False),
        ([1.40, 1.50, 1.40], 5.0, 0.1, True),
        # Two LSBs is a real move, not quantization.
        ([1.30, 1.60], 5.0, 0.1, False),
        # Percentage still wins at higher load (5% of 20 W = 1 W).
        ([19.9, 20.1], 5.0, 0.1, True),
        ([19.0, 21.0], 5.0, 0.1, False),
    ],
)
def test_is_flat_uses_the_larger_of_percent_and_watt_floor(
    values: list[float],
    tolerance_pct: float,
    tolerance_w: float,
    expected: bool,
) -> None:
    assert MeasureUtil._is_flat(values, tolerance_pct, tolerance_w) is expected  # noqa: SLF001


@patch("time.time")
def test_wait_for_plateau_treats_a_witness_lsb_flicker_as_flat(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """A 0.1 W witness (Shelly) flipping 1.40 <-> 1.50 is one LSB, not a trend.

    Percent-only settle at 5% cannot accept that (~7% of 1.5 W) and waits out the
    cap; the watt floor is what lets the composite actually settle.
    """
    mock_time.side_effect = [0.0, 0.0, 0.5, 1.0]
    power_meter = MagicMock(PowerMeter)
    witness_values = [1.50, 1.40, 1.50]

    def fake_get_power(*_args: object, **_kwargs: object) -> PowerMeasurementResult:
        index = power_meter.get_power.call_count - 1
        power_meter.last_reading = SimpleNamespace(
            witnesses=[SimpleNamespace(name="shelly", corrected=witness_values[index])],
        )
        return PowerMeasurementResult(power=1.41, updated=0.0)

    power_meter.get_power.side_effect = fake_get_power
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=lambda _seconds: None)

    elapsed = measure_util.wait_for_plateau(
        5.0,
        tolerance_pct=5.0,
        tolerance_w=0.1,
        window_seconds=1.0,
        poll_interval=0.5,
    )

    assert elapsed == 1.0


@patch("time.time")
def test_wait_for_plateau_stops_once_the_reading_is_flat_for_the_window(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    # Readings drift, then hold flat within tolerance for the whole trailing window.
    mock_time.side_effect = [0.0, 0.0, 0.5, 1.0, 1.5, 2.0]
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.side_effect = [
        PowerMeasurementResult(power=10.0, updated=0.0),
        PowerMeasurementResult(power=5.0, updated=0.0),
        PowerMeasurementResult(power=5.02, updated=0.0),
        PowerMeasurementResult(power=4.99, updated=0.0),
    ]
    waited: list[float] = []
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=waited.append)

    elapsed = measure_util.wait_for_plateau(10.0, tolerance_pct=2.0, window_seconds=1.0, poll_interval=0.5)

    assert elapsed == 1.5
    assert power_meter.get_power.call_count == 4
    assert waited == [0.5, 0.5, 0.5]


@patch("time.time")
def test_wait_for_plateau_gives_up_at_max_wait_if_never_stable(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    # Keeps drifting past the tolerance the whole time; must fall back to the cap.
    mock_time.side_effect = [0.0, 0.0, 0.5, 1.0, 1.5, 2.0]
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.side_effect = [PowerMeasurementResult(power=1.0 + index, updated=0.0) for index in range(5)]
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=lambda _seconds: None)

    elapsed = measure_util.wait_for_plateau(2.0, tolerance_pct=1.0, window_seconds=1.0, poll_interval=0.5)

    assert elapsed == 2.0


@patch("time.time")
def test_wait_for_plateau_treats_read_errors_as_not_yet_stable(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    mock_time.side_effect = [0.0, 0.0, 0.5, 1.0, 1.5]
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.side_effect = [
        ApiConnectionError("no reading yet"),
        PowerMeasurementResult(power=5.0, updated=0.0),
        PowerMeasurementResult(power=5.0, updated=0.0),
        PowerMeasurementResult(power=5.0, updated=0.0),
    ]
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=lambda _seconds: None)

    elapsed = measure_util.wait_for_plateau(5.0, tolerance_pct=1.0, window_seconds=1.0, poll_interval=0.5)

    assert elapsed == 1.5


@patch("time.time")
def test_wait_for_plateau_waits_for_a_composite_witness_to_flatten_too(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """A composite meter's primary can plateau on a stale/slow value while an independent
    witness is still visibly trending toward it -- indistinguishable from "settled" if
    only the primary's own readings are checked. Regression test for a real run (2026-09-06)
    where exactly this happened: the primary held flat at 3.90 W for a full settle window
    while the OCR witness was still climbing 3.71 -> 3.78 -> 3.79 -> 3.80 -> 3.81 W.
    """
    mock_time.side_effect = [0.0, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    power_meter = MagicMock(PowerMeter)
    # Primary is flat at 5.0 W from the very first poll -- alone, it would settle after
    # the first full window (t=1.0).
    witness_values = [1.0, 2.0, 3.0, 3.9, 4.0, 4.0]

    def fake_get_power(*_args: object, **_kwargs: object) -> PowerMeasurementResult:
        index = power_meter.get_power.call_count - 1
        power_meter.last_reading = SimpleNamespace(
            witnesses=[SimpleNamespace(name="ocr", corrected=witness_values[index])],
        )
        return PowerMeasurementResult(power=5.0, updated=0.0)

    power_meter.get_power.side_effect = fake_get_power
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=lambda _seconds: None)

    elapsed = measure_util.wait_for_plateau(3.0, tolerance_pct=3.0, window_seconds=1.0, poll_interval=0.5)

    # The witness only flattens out on the last two polls (4.0, 4.0 W) -- it, not the
    # primary, is what gates settlement here.
    assert elapsed == 2.5


@patch("time.time")
def test_wait_for_plateau_ignores_a_witness_that_never_reads(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """A witness that errors on every poll (e.g. an optional, non-required one that's
    currently unreachable) reports no `corrected` value at all -- it must not block
    settlement forever just because it's never in `witness_readings`.
    """
    mock_time.side_effect = [0.0, 0.0, 0.5, 1.0]
    power_meter = MagicMock(PowerMeter)

    def fake_get_power(*_args: object, **_kwargs: object) -> PowerMeasurementResult:
        power_meter.last_reading = SimpleNamespace(
            witnesses=[SimpleNamespace(name="ocr", corrected=None, error="camera unavailable")],
        )
        return PowerMeasurementResult(power=5.0, updated=0.0)

    power_meter.get_power.side_effect = fake_get_power
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=lambda _seconds: None)

    elapsed = measure_util.wait_for_plateau(5.0, tolerance_pct=1.0, window_seconds=1.0, poll_interval=0.5)

    assert elapsed == 1.0


@patch("time.time")
def test_wait_for_plateau_ignores_a_flat_zero_until_power_appears(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """A group still off is a perfect 0 W plateau; that must not count as settled."""

    mock_time.side_effect = [0.0, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.side_effect = [
        PowerMeasurementResult(power=0.0, updated=0.0),
        PowerMeasurementResult(power=0.0, updated=0.0),
        PowerMeasurementResult(power=3.1, updated=0.0),
        PowerMeasurementResult(power=3.12, updated=0.0),
        PowerMeasurementResult(power=3.11, updated=0.0),
    ]
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=lambda _seconds: None)

    elapsed = measure_util.wait_for_plateau(
        10.0,
        tolerance_pct=2.0,
        window_seconds=1.0,
        poll_interval=0.5,
        min_power=0.05,
    )

    assert elapsed == 2.0


@patch("time.time")
def test_wait_for_plateau_accepts_a_stable_reading_when_polls_are_slower_than_the_old_sliver(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    """0.35 s spacing (0.25 s interval + Shelly RTT) used to miss the 0.9-1.0 s
    oldest-sample sliver and wait out the cap on a dead-flat 22 W reading.
    """
    mock_time.side_effect = [0.0, 0.35, 0.70, 1.05, 1.40, 1.75]
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.return_value = PowerMeasurementResult(power=22.11, updated=0.0)
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=lambda _seconds: None)

    elapsed = measure_util.wait_for_plateau(
        20.0,
        tolerance_pct=10.0,
        tolerance_w=0.1,
        window_seconds=1.0,
        poll_interval=0.25,
    )

    assert elapsed == pytest.approx(1.05)
    assert power_meter.get_power.call_count == 3


@patch("time.time")
def test_wait_for_plateau_keeps_waiting_while_the_trailing_window_is_still_climbing(
    mock_time: MagicMock,
    mock_config_factory: MockConfigFactory,
) -> None:
    mock_time.side_effect = [0.0, 0.35, 0.70, 1.05, 1.40, 1.75, 2.10, 2.45, 2.80, 3.15, 3.50]
    power_meter = MagicMock(PowerMeter)
    power_meter.get_power.side_effect = [
        PowerMeasurementResult(power=watt, updated=0.0)
        for watt in (12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 22.05, 22.08, 22.10, 22.11)
    ]
    measure_util = MeasureUtil(power_meter, mock_config_factory(), wait=lambda _seconds: None)

    elapsed = measure_util.wait_for_plateau(
        20.0,
        tolerance_pct=2.0,
        tolerance_w=0.1,
        window_seconds=1.0,
        poll_interval=0.25,
    )

    assert elapsed == pytest.approx(2.80)
    assert power_meter.get_power.call_count == 8


def test_stale_timestamp_and_witness_mismatch_do_not_reconnect_the_camera() -> None:
    assert _should_reconnect_meter(OutdatedMeasurementError("Power reading is from before this point started")) is False
    assert _should_reconnect_meter(WitnessDisagreementError("ocr 14.2 W but shelly=12.4 W")) is False
    assert _should_reconnect_meter(OutdatedMeasurementError("OCR: last accepted reading is 5.5s old")) is True
    assert _should_reconnect_meter(WaitingForMeterError("OCR: waiting for a readable frame")) is False
