from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime as dt
import logging
from statistics import mean
import time

from measure.cancellation import MeasurementCancelledError
from measure.const import (
    DUMMY_LOAD_TREND_RELATIVE_THRESHOLD,
    RETRY_COUNT_LIMIT,
    Trend,
)
from measure.powermeter.errors import (
    OutdatedMeasurementError,
    PowerMeterError,
    UnsupportedFeatureError,
    WaitingForMeterError,
    WitnessDisagreementError,
    ZeroReadingError,
)
from measure.powermeter.powermeter import PowerMeasurementResult, PowerMeter
from measure.tuning import MeasurementParameters

_LOGGER = logging.getLogger("measure")


#: Independent polls needed in the trailing window before a track can settle.
#: 3 is one second of 0.25–0.35 s polls (interval + a Shelly RTT) without asking
#: for a sample exactly 0.9–1.0 s old — that sliver is what used to deadlock.
SETTLE_MIN_SAMPLES = 3


def _trim_to_window(readings: list[tuple[float, float]], now: float, window_seconds: float) -> None:
    while len(readings) > 1 and now - readings[0][0] > window_seconds:
        readings.pop(0)


def _track_has_settled(
    readings: list[tuple[float, float]],
    now: float,
    started_at: float,
    window_seconds: float,
    tolerance_pct: float,
    tolerance_w: float,
    min_samples: int = SETTLE_MIN_SAMPLES,
) -> bool:
    """True when this track has been watched for ``window_seconds``, has enough
    samples in that trailing window, and those samples sit within the band.

    Time is wall-clock from ``started_at``, not the span of the kept samples.
    Requiring the oldest kept sample to sit in a 100 ms sliver (0.9–1.0 s) was
    how a perfectly flat 22 W reading still waited 7–20 s.
    """
    if now - started_at < window_seconds:
        return False
    window = [value for timestamp, value in readings if now - timestamp <= window_seconds]
    return len(window) >= min_samples and MeasureUtil._is_flat(window, tolerance_pct, tolerance_w)  # noqa: SLF001


def _should_reconnect_meter(error: PowerMeterError) -> bool:
    """Reconnecting the camera does not fix a timestamp race, a wait, or a witness mismatch."""

    if isinstance(error, (WitnessDisagreementError, WaitingForMeterError)):
        return False
    return "from before this point started" not in str(error)


class MeasurementError(PowerMeterError):
    """Base error for invalid or incomplete measurement results."""


class NoValidReadingsError(MeasurementError):
    """Raised when a measurement completes without a usable reading."""


class DummyLoadMeasurementError(MeasurementError):
    """Raised when a dummy-load measurement cannot produce a valid result."""


@dataclass(frozen=True)
class MeasurementResult:
    power: float
    voltages: list[float]


@dataclass(frozen=True)
class AverageMeasurementConvergence:
    min_duration: int
    window_duration: int
    absolute_threshold: float
    relative_threshold: float


@dataclass(frozen=True)
class AverageMeasurementSnapshot:
    elapsed: float
    average: float


@dataclass
class AverageMeasurementState:
    start_time: float
    readings: list[float]
    snapshots: list[AverageMeasurementSnapshot]
    voltages: list[float]
    consecutive_errors: int = 0
    interrupted: bool = False


class MeasureUtil:
    on_accepted_reading: Callable[[MeasurementResult], None] | None = None

    def __init__(
        self,
        power_meter: PowerMeter,
        parameters: MeasurementParameters,
        include_voltage: Callable[[], bool] | None = None,
        wait: Callable[[float], None] = time.sleep,
        on_sample: Callable[[float], None] | None = None,
        on_calibration_sample: Callable[[float, float, float], None] | None = None,
        on_accepted_reading: Callable[[MeasurementResult], None] | None = None,
    ) -> None:
        self.power_meter = power_meter
        self.dummy_load_value: float | None = None
        self.config = parameters
        self._include_voltage = include_voltage or (lambda: False)
        self._wait = wait
        self._on_sample = on_sample
        self._on_calibration_sample = on_calibration_sample
        self.on_accepted_reading = on_accepted_reading

    def take_average_measurement(
        self,
        duration: int,
        measure_resistance: bool = False,
        convergence: AverageMeasurementConvergence | None = None,
        on_progress: Callable[[float, float], None] | None = None,
        *,
        finish_on_interrupt: bool = False,
    ) -> MeasurementResult:
        """Average valid readings; only standalone averaging may finish early on operator stop."""
        _LOGGER.info("Measuring average %s over %s seconds", "resistance" if measure_resistance else "power", duration)
        state = self._collect_average_measurements(
            duration,
            measure_resistance,
            convergence,
            on_progress,
            finish_on_interrupt,
        )
        if on_progress is not None:
            elapsed = min(duration, max(0.0, time.time() - state.start_time)) if state.interrupted else duration
            on_progress(elapsed, duration)

        if not state.readings:
            raise NoValidReadingsError("No valid readings were recorded")

        average = round(mean(state.readings), 2)
        _LOGGER.info(
            "Average of %d measurements: %.2f %s",
            len(state.readings),
            average,
            "Ω" if measure_resistance else "W",
        )
        return MeasurementResult(power=average, voltages=state.voltages)

    def _collect_average_measurements(
        self,
        duration: int,
        measure_resistance: bool,
        convergence: AverageMeasurementConvergence | None,
        on_progress: Callable[[float, float], None] | None = None,
        finish_on_interrupt: bool = False,
    ) -> AverageMeasurementState:
        start_time = time.time()
        state = AverageMeasurementState(start_time, [], [], [])
        first_measurement = True

        try:
            while (time.time() - start_time) < duration:
                if not first_measurement and not self._sleep_before_next_average_reading(start_time, duration):
                    break
                first_measurement = False
                if self._collect_average_measurement(state, duration, measure_resistance, convergence, on_progress):
                    break
        except KeyboardInterrupt, MeasurementCancelledError:
            # Never turn an interrupted calibration/profile point or an empty run into a valid result.
            if not finish_on_interrupt or not state.readings:
                raise
            state.interrupted = True
            _LOGGER.info("Stopped averaging; keeping %d valid readings", len(state.readings))

        return state

    def _collect_average_measurement(
        self,
        state: AverageMeasurementState,
        duration: int,
        measure_resistance: bool,
        convergence: AverageMeasurementConvergence | None,
        on_progress: Callable[[float, float], None] | None,
    ) -> bool:
        if on_progress is not None:
            on_progress(time.time() - state.start_time, duration)
        try:
            result = self._take_average_measurement_reading(measure_resistance)
        except WaitingForMeterError as error:
            _LOGGER.info("%s", error)
            self._wait(0.25)
            return False
        except PowerMeterError as error:
            if self._average_measurement_retry_limit_reached(state, error):
                raise
            if _should_reconnect_meter(error):
                self._recover_power_meter(error)
            return False
        return self._record_average_measurement_result(state, result, convergence)

    def _average_measurement_retry_limit_reached(self, state: AverageMeasurementState, error: PowerMeterError) -> bool:
        state.consecutive_errors += 1
        _LOGGER.warning(
            "Error during average measurement (attempt %d/%d): %s",
            state.consecutive_errors,
            self.config.max_retries,
            error,
        )
        return state.consecutive_errors > self.config.max_retries

    def _record_average_measurement_result(
        self,
        state: AverageMeasurementState,
        result: MeasurementResult | None,
        convergence: AverageMeasurementConvergence | None,
    ) -> bool:
        if result is None:
            return False

        state.consecutive_errors = 0
        state.readings.append(result.power)
        state.voltages.extend(result.voltages)
        self._append_average_snapshot(state.start_time, state.readings, state.snapshots)
        if self.on_accepted_reading is not None:
            self.on_accepted_reading(result)
        return bool(convergence and self.average_has_converged(state.snapshots, convergence))

    @staticmethod
    def _append_average_snapshot(
        start_time: float,
        readings: list[float],
        snapshots: list[AverageMeasurementSnapshot],
    ) -> None:
        """Record the cumulative average at the current elapsed measurement time."""
        snapshots.append(
            AverageMeasurementSnapshot(
                elapsed=time.time() - start_time,
                average=mean(readings),
            ),
        )

    @staticmethod
    def average_has_converged(
        snapshots: list[AverageMeasurementSnapshot],
        convergence: AverageMeasurementConvergence,
    ) -> bool:
        """Check whether the cumulative average is stable over the configured lookback window."""
        current = snapshots[-1]
        if current.elapsed < convergence.min_duration:
            return False

        comparison_elapsed = current.elapsed - convergence.window_duration
        comparison = next(
            (snapshot for snapshot in reversed(snapshots[:-1]) if snapshot.elapsed <= comparison_elapsed),
            None,
        )
        if comparison is None:
            return False

        delta = abs(current.average - comparison.average)
        if delta <= convergence.absolute_threshold:
            _LOGGER.info(
                "Average converged after %.1f seconds: %.2f W changed %.2f W over %.1f seconds",
                current.elapsed,
                current.average,
                delta,
                convergence.window_duration,
            )
            return True

        if comparison.average == 0:
            return False

        relative_delta = delta / abs(comparison.average)
        if relative_delta <= convergence.relative_threshold:
            _LOGGER.info(
                "Average converged after %.1f seconds: %.2f W changed %.2f%% over %.1f seconds",
                current.elapsed,
                current.average,
                relative_delta * 100,
                convergence.window_duration,
            )
            return True

        return False

    def _take_average_measurement_reading(self, measure_resistance: bool) -> MeasurementResult | None:
        """Take one reading using the average-measurement mode selected for this run."""
        if measure_resistance:
            return self._take_resistance_reading()
        return self._read_power(ignore_zero=True)

    def _sleep_before_next_average_reading(self, start_time: float, duration: int) -> bool:
        if (time.time() - start_time + self.config.sleep_time) >= duration:
            return False
        self._wait(self.config.sleep_time)
        return True

    def _take_resistance_reading(self) -> MeasurementResult | None:
        result = self.power_meter.get_power(include_voltage=True)
        power, voltage = result.power, result.voltage

        if voltage is None or voltage < 1:
            raise ZeroReadingError("Voltage measurement returned zero")

        if round(power, 2) == 0:
            _LOGGER.warning("Invalid measurement: power: %.2f W, voltage: %.2f", power, voltage)
            return None

        resistance = round((voltage**2) / power, 4)
        _LOGGER.debug("Measured resistance: %.2f Ω; measured power: %.2f W, voltage: %.2f", resistance, power, voltage)
        self._emit_calibration_sample(power, resistance, voltage)
        return MeasurementResult(power=resistance, voltages=[voltage])

    def wait_for_plateau(
        self,
        max_wait: float,
        *,
        tolerance_pct: float,
        tolerance_w: float = 0.0,
        window_seconds: float = 1.0,
        poll_interval: float = 0.25,
        min_power: float = 0.0,
    ) -> float:
        """Poll until the reading has stopped moving, or `max_wait` elapses.

        Settled means: watched for at least `window_seconds`, at least
        `SETTLE_MIN_SAMPLES` values in that trailing window, and those values
        sit within `tolerance_pct` / `tolerance_w`. A read error is "not yet
        stable" — the meter often has no fresh sample right after a light
        change. Only the trailing window is checked for spread, so an early
        climb does not have to re-settle from scratch once it holds.
        """
        # Purely a logging label (see CompositePowerMeter.log_context) so these throwaway
        # polls read as "settling" in the log instead of looking identical to an accepted
        # sample -- restored afterward since the same meter instance is reused for the
        # samples that follow.
        had_log_context = hasattr(self.power_meter, "log_context")
        if had_log_context:
            self.power_meter.log_context = "settling"  # type: ignore[attr-defined]
        try:
            start = time.time()
            readings: list[tuple[float, float]] = []
            # Keyed by witness name, populated only when `self.power_meter` is a
            # `CompositePowerMeter` -- see the flatness check below for why these matter.
            witness_readings: dict[str, list[tuple[float, float]]] = {}
            while True:
                try:
                    power = self.power_meter.get_power().power
                except PowerMeterError:
                    # A witness disagreement raises here too, but `_record_witness_readings`
                    # below still sees this poll's witness values -- recorded on the meter
                    # before that check ran, so they're valid flatness evidence even when
                    # the poll as a whole gets treated as "no primary reading yet".
                    power = None
                # Stamp after the read so a slow meter (Shelly RTT, OCR) does not
                # push consecutive samples more than a window apart on paper.
                now = time.time()
                elapsed = now - start
                if power is not None and power + 1e-9 >= min_power:
                    readings.append((now, power))
                self._record_witness_readings(witness_readings, now)
                _trim_to_window(readings, now, window_seconds)
                for track in witness_readings.values():
                    _trim_to_window(track, now, window_seconds)
                # A composite meter's primary alone plateauing isn't enough: the primary
                # can sit on a stale or slow-to-update value while an independent witness
                # is still visibly trending toward it, which looks identical to "settled"
                # from the primary's own readings alone (confirmed against a real run's
                # raw samples 2026-09-06). Requiring every witness's own track to be
                # settled too catches that.
                if _track_has_settled(
                    readings, now, start, window_seconds, tolerance_pct, tolerance_w
                ) and all(
                    _track_has_settled(track, now, start, window_seconds, tolerance_pct, tolerance_w)
                    for track in witness_readings.values()
                ):
                    return elapsed
                if elapsed >= max_wait:
                    return elapsed
                self._wait(min(poll_interval, max(0.0, max_wait - elapsed)))
        finally:
            if had_log_context:
                self.power_meter.log_context = "reading"  # type: ignore[attr-defined]

    def _record_witness_readings(self, witness_readings: dict[str, list[tuple[float, float]]], now: float) -> None:
        """Append this poll's corrected witness values, if `self.power_meter` is composite."""

        last_reading = getattr(self.power_meter, "last_reading", None)
        if last_reading is None:
            return
        for witness in last_reading.witnesses:
            if witness.corrected is not None:
                witness_readings.setdefault(witness.name, []).append((now, witness.corrected))

    @staticmethod
    def _is_flat(values: list[float], tolerance_pct: float, tolerance_w: float = 0.0) -> bool:
        if len(values) < 2:
            return False
        spread = max(values) - min(values)
        scale = max((abs(value) for value in values), default=0.0) or 1.0
        # Same shape as witness agreement: relative band *or* an absolute floor. The
        # floor is what lets a 0.1 W meter (Shelly) settle at low load, where one LSB
        # is already several percent of the reading.
        allowed = max(scale * tolerance_pct / 100.0, tolerance_w)
        return spread <= allowed + 1e-9

    def take_measurement(
        self,
        start_timestamp: float | None = None,
        retry_count: int = 0,
    ) -> MeasurementResult:
        """Get a measurement from the powermeter, take multiple samples and calculate the average"""

        measurements: list[float] = []
        voltages: list[float] = []
        accepted: list[MeasurementResult] = []
        has_log_context = hasattr(self.power_meter, "log_context")
        # Whether this measurement was already labeled "settled"/"settle cap hit" by
        # LightRunner._settle() just before this call -- that label says *why* this
        # reading is being accepted (it held flat vs. we gave up waiting) and is strictly
        # more informative than a generic "accepted", so it's left alone rather than
        # overwritten below. Callers with no settle step of their own (charging/fan/
        # recorder/speaker runners, the light-load preflight probe) never set it, so
        # their accepted samples fall back to the previous generic labeling unchanged.
        settle_outcome_labeled = has_log_context and getattr(self.power_meter, "log_context", None) in {
            "settled",
            "settle cap hit",
        }
        # Take multiple samples to reduce noise
        for i in range(1, self.config.sample_count + 1):
            _LOGGER.debug("Taking sample %d", i)
            if has_log_context and not settle_outcome_labeled:
                # Distinguishes an accepted sample from the "settling" polls that preceded
                # it in the log -- both otherwise emit an identical-looking Meters line.
                sample_count = self.config.sample_count
                label = "accepted" if sample_count == 1 else f"accepted, sample {i}/{sample_count}"
                self.power_meter.log_context = label  # type: ignore[attr-defined]
            stale_waits = 0
            while True:
                try:
                    result = self._read_power(start_timestamp=start_timestamp)
                    break
                except WaitingForMeterError as error:
                    _LOGGER.info("%s", error)
                    self._wait(0.25)
                except OutdatedMeasurementError as error:
                    # A silent camera or a reading from before this point can clear
                    # itself. Do not burn the session's max_retries on that.
                    stale_waits += 1
                    if stale_waits == 1 or stale_waits % 20 == 0:
                        _LOGGER.warning(
                            "%s; waiting for a fresh reading (attempt %d)",
                            error,
                            stale_waits,
                        )
                    if _should_reconnect_meter(error):
                        self._recover_power_meter(error)
                    self._wait(max(0.25, self.config.sleep_time))
                except PowerMeterError as error:
                    return self._retry_measurement_or_raise(error, start_timestamp, retry_count)
            assert result is not None
            measurements.append(result.power)
            voltages.extend(result.voltages)
            accepted.append(result)

            if self.config.sample_count > 1:
                self._wait(self.config.sleep_time_sample)

        # Determine Average PM reading
        if not measurements:
            raise NoValidReadingsError("No valid readings were recorded")

        average = mean(measurements)
        if self.config.sample_count > 1:
            _LOGGER.info("Average measurement: %.3f W", average)
        else:
            _LOGGER.info("Measurement: %.3f W", average)
        if self.on_accepted_reading is not None:
            for result in accepted:
                self.on_accepted_reading(result)
        return MeasurementResult(power=average, voltages=voltages)

    def _read_power(
        self,
        *,
        start_timestamp: float | None = None,
        ignore_zero: bool = False,
    ) -> MeasurementResult | None:
        """Read and validate one power sample for every measurement mode."""
        include_voltage = self.dummy_load_value is not None or self._include_voltage()
        measurement = self.power_meter.get_power(include_voltage=include_voltage)
        updated_at = dt.fromtimestamp(measurement.updated).strftime("%d-%m-%Y, %H:%M:%S")
        _LOGGER.debug("Measurement received (update_time=%s)", updated_at)
        if start_timestamp and measurement.updated < start_timestamp:
            started_at = dt.fromtimestamp(start_timestamp).strftime("%d-%m-%Y, %H:%M:%S")
            raise OutdatedMeasurementError(
                f"Power reading is from before this point started (reading {updated_at}, point started {started_at})",
            )

        power = measurement.power
        voltages = self._get_voltages(measurement)
        if self.dummy_load_value:
            voltage = measurement.voltage
            if voltage is None or voltage < 1:
                raise ZeroReadingError("0 Volt was read from the power meter")
            power -= (voltage**2) / self.dummy_load_value
            if round(power, 2) <= 0:
                raise DummyLoadMeasurementError(
                    "Dummy-load correction produced non-positive target power; "
                    "verify the selected calibration and wiring",
                )
        elif round(power, 2) <= 0:
            if ignore_zero:
                _LOGGER.warning("Invalid measurement. Consumption: %.2f W; ignoring", power)
                return None
            raise ZeroReadingError("0 watt was read from the power meter")

        _LOGGER.info("Measured power: %.2f W", power)
        self._emit_sample(power)
        return MeasurementResult(power=power, voltages=voltages)

    def _retry_measurement_or_raise(
        self,
        error: PowerMeterError,
        start_timestamp: float | None,
        retry_count: int,
    ) -> MeasurementResult:
        if retry_count == self.config.max_retries:
            raise error
        if retry_count >= RETRY_COUNT_LIMIT:
            _LOGGER.error(
                "Retry count exceeded %d. Configured max_retries value: %d. Aborting to prevent infinite loop.",
                RETRY_COUNT_LIMIT,
                self.config.max_retries,
            )
            raise error
        if _should_reconnect_meter(error):
            self._recover_power_meter(error)
        else:
            _LOGGER.info("Retrying power reading without reconnecting the meter: %s", error)
        self._wait(self.config.sleep_time)
        return self.take_measurement(start_timestamp, retry_count + 1)

    def _recover_power_meter(self, error: PowerMeterError) -> None:
        """Reconnect a stuck meter/OCR source before the next retry, not the light."""
        _LOGGER.warning("Recovering power meter after: %s", error)
        try:
            self.power_meter.recover()
        except Exception:
            _LOGGER.debug("Power meter recover failed", exc_info=True)

    @staticmethod
    def dummy_load_trend(averages: list[float]) -> Trend | None:
        """Classify resistance readings as increasing, decreasing or steady."""
        if len(averages) < 20:
            return None

        mid = len(averages) // 2  # Calculate the midpoint

        first_half = averages[:mid]
        second_half = averages[mid:]

        first_slope = MeasureUtil._linear_slope(first_half)
        second_slope = MeasureUtil._linear_slope(second_half)

        threshold = mean(averages) * DUMMY_LOAD_TREND_RELATIVE_THRESHOLD

        def trend_direction(slope: float) -> Trend:
            if slope > threshold:
                return Trend.INCREASING
            if slope < -threshold:
                return Trend.DECREASING
            return Trend.STEADY

        first_trend = trend_direction(first_slope)
        second_trend = trend_direction(second_slope)

        if first_trend == second_trend:
            return first_trend
        if first_trend == Trend.STEADY:
            return second_trend
        if second_trend == Trend.STEADY:
            return first_trend
        return Trend.UNSTABLE

    @staticmethod
    def _linear_slope(values: list[float]) -> float:
        """Return the least-squares slope for equally spaced values without NumPy."""
        if len(values) < 2:
            return 0.0
        mean_x = (len(values) - 1) / 2
        mean_y = mean(values)
        numerator = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
        denominator = sum((index - mean_x) ** 2 for index in range(len(values)))
        return numerator / denominator

    def validate_dummy_load_support(self) -> None:
        """Require voltage measurements before configuring a dummy load."""
        if not self.power_meter.has_voltage_support():
            raise UnsupportedFeatureError(
                "The selected power meter does not support voltage measurements required for dummy loads",
            )

    def set_dummy_load_resistance(self, resistance: float) -> None:
        """Apply a known physical dummy-load resistance to subsequent power readings."""
        self.validate_dummy_load_support()
        if resistance <= 0:
            raise DummyLoadMeasurementError("Dummy-load resistance must be positive")
        self.dummy_load_value = resistance

    def _emit_sample(self, power: float) -> None:
        if self._on_sample is None:
            return
        try:
            self._on_sample(power)
        except Exception:  # live feedback must not break a measurement
            _LOGGER.debug("Failed to emit live power sample", exc_info=True)

    def _emit_calibration_sample(self, power: float, resistance: float, voltage: float) -> None:
        if self._on_calibration_sample is None:
            return
        try:
            self._on_calibration_sample(power, resistance, voltage)
        except Exception:  # live feedback must not break a measurement
            _LOGGER.debug("Failed to emit live dummy-load calibration sample", exc_info=True)

    @staticmethod
    def _get_voltages(measurement: PowerMeasurementResult) -> list[float]:
        if measurement.voltage is None:
            return []
        return [measurement.voltage]
