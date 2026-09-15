import csv
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime as dt
import gzip
import logging
import os
import shutil
import time
from typing import Any, Literal, TextIO

from measure.controller.errors import ApiConnectionError, ControllerError
from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightController, LightInfo
from measure.execution import ImmediateInteraction, LightOperatingPoint, RunInteraction
from measure.home_assistant import explain_home_assistant_error
from measure.powermeter.composite import CompositePowerMeter, CompositeReading
from measure.powermeter.errors import (
    OutdatedMeasurementError,
    PowerMeterError,
    StandbyLikeOnError,
    ZeroReadingError,
)
from measure.powermeter.spec import DummyPowerMeterSpec
from measure.request import LightMeasurementRequest, ResumePolicy
from measure.runner.errors import RunnerError
from measure.runner.light_plan import (
    CSV_HEADERS,
    ColorTempVariation,
    EffectVariation,
    HsVariation,
    LightMeasurementPlan,
    LightModePlan,
    Variation,
    build_light_plan,
    estimate_light_time_left,
    low_load_probe_variations,
    premeasure_emitter_bounds,
    variation_from_csv_row,
    variations_after,
)
from measure.runner.on_off_bounds import (
    OnOffBounds,
    closer_to_standby_than_minimum_on,
    load_on_off_bounds,
    minimum_on_from_seed,
    on_load_distinguishable_from_standby,
    save_on_off_bounds,
    seed_supports_standby,
)
from measure.runner.light_setup import set_light_to_maximum_brightness
from measure.runner.lut_csv import LutRow, load_mode_rows, missing_variations, union_rows, write_mode_csv
from measure.runner.raw_sample_writer import RawSampleWriter
from measure.runner.runner import MeasurementRunner, RunnerResult
from measure.runner.smart_envelope import (
    MeasuredPoint,
    next_smart_batch,
    replay_smart_remaining,
    smart_applies,
    smart_uninvented_count,
)
from measure.tuning import MeasurementParameters
from measure.util.measure_util import AverageMeasurementConvergence, MeasurementResult, MeasureUtil

MAX_CONSECUTIVE_ZERO_READINGS = 5
# Long enough that a Matter session timeout (~61s on HA) can finish before we
# poke the same lights again. Shorter waits stacked Operation aborted on top
# of the still-running first call.
_LIGHT_CHANGE_RETRY_WAIT = 15.0
REASON_WARMUP = (
    "Issue #2598: some lights drop the first on-command after being off, "
    "so maximum brightness is sent twice before sampling starts."
)
ZERO_READING_ABORT_MESSAGE = (
    "Aborting measurement session after repeated 0 W readings. The power meter may not resolve this low load. "
    "Verify the device is on and connected, measure multiple identical lights together, "
    "add a resistive dummy load, or use a more sensitive meter. "
    "See https://docs.powercalc.nl/contributing/measure/troubleshooting/ for troubleshooting guidance."
)

_LOGGER = logging.getLogger("measure")


class LightRunner(MeasurementRunner[LightMeasurementRequest]):
    """Measure configured light modes and write one LUT CSV per mode."""

    def __init__(
        self,
        measure_util: MeasureUtil,
        parameters: MeasurementParameters,
        light_controller: LightController,
        interaction: RunInteraction | None = None,
        *,
        resume: bool = False,
    ) -> None:
        self.light_controller = light_controller
        self.measure_util = measure_util
        self.lut_modes: set[LutMode] | None = None
        self.num_lights: int = 1
        self.num_0_readings: int = 0
        self.skipped_zero_readings: int = 0
        self.light_info: LightInfo | None = None
        self.plan: LightMeasurementPlan | None = None
        self.active_plan: LightMeasurementPlan | None = None
        self.config = parameters
        self.gzip = True
        self.interaction = interaction or ImmediateInteraction()
        self._resume = resume
        self._extend = False
        self._remeasure_existing = False
        self._raw_writer: RawSampleWriter | None = None
        self._raw_mode: LutMode | None = None
        self._raw_variation: Variation | None = None
        self._raw_lines_this_point = 0
        # Only meaningful when settle_tolerance_pct > 0 -- None otherwise, since a fixed
        # wait has no "did it hit the cap" question to answer. Read by run_mode() right
        # after wait() to attach to that variation's raw sample.
        self.last_settle_seconds: float | None = None
        self.last_settle_hit_cap: bool | None = None
        self._smart_stage: str | None = None
        self._smart_reason: str | None = None
        self._already_measured_count = 0
        self._seed_points_by_mode: dict[LutMode, list[MeasuredPoint]] = {}
        self._session_started_at: float | None = None
        self._on_off_bounds: OnOffBounds | None = None
        self._standby_voltages: list[float] = []
        self._standby_like_count = 0
        self._export_directory = ""

    def _known_on_watts(self) -> list[float]:
        return [
            point.watt for points in self._seed_points_by_mode.values() for point in points if point.variation.bri > 0
        ]

    def _establish_on_off_bounds(self) -> None:
        """Record standby (and low-load ons when there is no seed) once; reuse after that."""

        known_on = self._known_on_watts()
        persisted = load_on_off_bounds(self._export_directory) if self._export_directory else None
        if persisted is not None:
            minimum_on = minimum_on_from_seed(known_on, persisted.standby) if known_on else persisted.minimum_on
            if minimum_on is None:
                minimum_on = persisted.minimum_on
            self._on_off_bounds = OnOffBounds(standby=persisted.standby, minimum_on=minimum_on)
            _LOGGER.info(
                "On/off bounds from prior recording: standby %.3f W/lamp, lowest on %s",
                self._on_off_bounds.standby,
                "unset" if self._on_off_bounds.minimum_on is None else f"{self._on_off_bounds.minimum_on:.3f} W/lamp",
            )
            return

        self.interaction.phase("Measuring standby power")
        standby_result = self._read_standby_power()
        standby = standby_result.power
        self._standby_voltages = list(standby_result.voltages)
        if known_on and not seed_supports_standby(known_on, standby):
            self.interaction.notify(
                f"Standby reading {standby:.3f} W/lamp is not below the known on-load "
                f"({min(known_on):.3f} W/lamp). Leaving the discard floor unset.",
                warning=True,
            )
            return
        if known_on:
            self.interaction.phase("Using lowest on-load from the prior measurement")
            minimum_on = minimum_on_from_seed(known_on, standby)
            if minimum_on is None:
                self.interaction.notify(
                    f"Every prior on-load reading is too close to standby ({standby:.3f} W/lamp). "
                    "Leaving the discard floor unset.",
                    warning=True,
                )
                return
        else:
            minimum_on = self._measure_lowest_on_load(standby)

        self._on_off_bounds = OnOffBounds(standby=standby, minimum_on=minimum_on)
        if self._export_directory:
            save_on_off_bounds(self._export_directory, self._on_off_bounds)
        _LOGGER.info(
            "On/off bounds: standby %.3f W/lamp, lowest on %s",
            self._on_off_bounds.standby,
            "unset" if self._on_off_bounds.minimum_on is None else f"{self._on_off_bounds.minimum_on:.3f} W/lamp",
        )

    def _measure_lowest_on_load(self, standby: float) -> float | None:
        """One low-load on-walk at the start of a fresh run, same settle/OCR rules as a LUT point."""

        assert self.plan is not None
        variations = low_load_probe_variations(self.plan, min_brightness=self.config.min_brightness)
        if not variations:
            return None
        on_loads: list[float] = []
        previous: Variation | None = None
        for variation in variations:
            self.interaction.phase("Checking lowest on-load")
            started = time.time()
            self._change_light_with_retry(variation.mode, variation)
            self.wait(variation, previous)
            previous = variation
            on_loads.append(self.take_power_measurement(variation.mode, started).power)
        minimum_on = min(on_loads)
        if not on_load_distinguishable_from_standby(minimum_on, standby):
            self.interaction.notify(
                f"The lowest on-load reading ({minimum_on:.3f} W/lamp) is too close to "
                f"standby ({standby:.3f} W/lamp). Leaving the discard floor unset.",
                warning=True,
            )
            return None
        return minimum_on

    def _raise_if_standby_like_on(self, variation: Variation, power: float) -> None:
        bounds = self._on_off_bounds
        if bounds is None or variation.bri <= 0:
            return
        if bounds.minimum_on is None:
            return
        if closer_to_standby_than_minimum_on(power, bounds.standby, bounds.minimum_on):
            raise StandbyLikeOnError(
                f"On-state reading {power:.3f} W is closer to standby ({bounds.standby:.3f} W) "
                f"than to the lowest on-load ({bounds.minimum_on:.3f} W). Lights are probably still off.",
            )

    def _wait(self, seconds: float) -> None:
        self.interaction.wait(seconds)

    def _checkpoint(self) -> None:
        self.interaction.checkpoint()

    def _configure(self, request: LightMeasurementRequest) -> None:
        if request.rated_power_w:
            self.config = replace(self.config, rated_power=request.rated_power_w)
        self.lut_modes = set(request.modes)
        self.num_lights = request.multiple_light_count
        self.gzip = request.gzip
        self._resume = request.resume_policy == ResumePolicy.RESUME
        self._extend = request.resume_policy == ResumePolicy.EXTEND
        self._remeasure_existing = request.remeasure_existing
        self.light_info = self.light_controller.get_light_info()
        effects = self.light_controller.get_effect_list()
        self.plan = build_light_plan(self.lut_modes, self.config, self.light_info, effects)
        self.active_plan = None

    def writes_export_files(self) -> bool:
        return True

    def cleanup(self) -> None:
        targets = _light_targets(self.light_controller)
        try:
            _LOGGER.info("Turning off %s after the measurement", targets)
            self.light_controller.change_light_state(LutMode.BRIGHTNESS, on=False)
        except Exception as error:  # noqa: BLE001 - cleanup must not mask the measurement outcome
            _LOGGER.warning(
                "Could not turn off %s during measurement cleanup: %s",
                targets,
                explain_home_assistant_error(error),
            )
        else:
            _LOGGER.info("Turned off %s", targets)
            self.interaction.operating_point(LightOperatingPoint(type="light", on=False))
        finally:
            try:
                self.light_controller.close()
            except Exception as error:  # noqa: BLE001 - cleanup must not mask the measurement outcome
                _LOGGER.warning("Could not close the light controller during measurement cleanup: %s", error)

    def run(self, request: LightMeasurementRequest, export_directory: str) -> RunnerResult:
        self._configure(request)
        assert self.plan is not None
        measurements_to_run = [
            self.prepare_measurements_for_mode(export_directory, mode_plan.mode) for mode_plan in self.plan.modes
        ]
        self.active_plan = LightMeasurementPlan(
            modes=[
                LightModePlan(mode=measurement.mode, variations=measurement.variations)
                for measurement in measurements_to_run
            ],
            effects=list(self.plan.effects),
        )

        self._already_measured_count = sum(len(measurement.seed_rows) for measurement in measurements_to_run)
        self._seed_points_by_mode = {
            measurement.mode: [MeasuredPoint(variation, row.watt) for variation, row in measurement.seed_rows.items()]
            for measurement in measurements_to_run
        }
        self._session_started_at = time.time()
        self._export_directory = export_directory
        if _should_establish_on_off_bounds(request):
            self._establish_on_off_bounds()
        all_variations: list[Variation] = []
        remaining_variations: list[Variation] = []
        for measurement in measurements_to_run:
            all_variations.extend(measurement.variations)
            remaining_variations.extend(measurement.variations)
        _LOGGER.info(
            "Total number of variations: %d (%d already measured)",
            self._already_measured_count + len(all_variations),
            self._already_measured_count,
        )
        voltages: list[float] = []

        for measurement_info in measurements_to_run:
            voltages.extend(self.run_mode(measurement_info, all_variations, remaining_variations))

        if remaining_variations:
            raise RunnerError(f"Measurement ended with {len(remaining_variations)} incomplete variations")
        if self._extend and not self._remeasure_existing and not all_variations:
            self.interaction.notify(
                "Refine found no new points at this Δ; the seed already covers the envelope.",
                warning=True,
            )

        return RunnerResult(
            model_json_data={
                "device_type": "light",
                "calculation_strategy": "lut",
            },
            voltages=voltages,
        )

    def prepare_measurements_for_mode(self, export_directory: str, mode: LutMode) -> MeasurementRunInput:
        """Fetch all variations for the given color mode and prepare the measurement session."""

        if mode == LutMode.WHITE:
            mode = LutMode.BRIGHTNESS

        csv_file_path = f"{export_directory}/{mode.value}.csv"

        assert self.plan is not None
        plan_variations = list(self.plan.for_mode(mode).variations)
        if self._extend:
            seed_rows = load_mode_rows(csv_file_path, mode)
            if self._remeasure_existing:
                variations = premeasure_emitter_bounds(plan_variations)
                is_resuming = False
            else:
                variations = missing_variations(plan_variations, seed_rows)
                is_resuming = bool(seed_rows)
            return MeasurementRunInput(
                mode=mode,
                csv_file=csv_file_path,
                variations=variations,
                is_resuming=is_resuming,
                seed_rows=seed_rows,
            )

        resume_at = None
        if self.should_resume(csv_file_path):
            resume_at = self.get_resume_variation(csv_file_path, mode)

        if resume_at is not None and smart_applies(mode, self.config) and self.light_info is not None:
            seed_rows = load_mode_rows(csv_file_path, mode)
            measured = [MeasuredPoint(variation, row.watt) for variation, row in seed_rows.items()]
            return MeasurementRunInput(
                mode=mode,
                csv_file=csv_file_path,
                variations=replay_smart_remaining(mode, measured, self.config, self.light_info),
                is_resuming=True,
                seed_rows=seed_rows,
            )

        variations = list(variations_after(plan_variations, resume_at))
        if resume_at is None:
            variations = premeasure_emitter_bounds(variations)
        return MeasurementRunInput(
            mode=mode,
            csv_file=csv_file_path,
            variations=variations,
            is_resuming=bool(resume_at),
        )

    def _resolve_white_mode(self, mode: LutMode) -> LutMode:
        """WHITE is measured as BRIGHTNESS after turning the light fully on."""
        if mode == LutMode.WHITE:
            self.light_controller.change_light_state(mode, on=True, bri=255)
            return LutMode.BRIGHTNESS
        return mode

    def run_mode(  # noqa: C901
        self,
        measurement_info: MeasurementRunInput,
        all_variations: list[Variation],
        remaining_variations: list[Variation],
    ) -> list[float]:
        """Run the measurement session for lights"""

        mode = self._resolve_white_mode(measurement_info.mode)
        voltages: list[float] = []

        file_write_mode, write_header_row = self._get_csv_write_options(measurement_info)

        _LOGGER.info(
            "Starting measurements. Estimated duration: %s",
            self.calculate_time_left(mode, remaining_variations),
        )

        raw_sample_writer = RawSampleWriter(f"{os.path.splitext(measurement_info.csv_file)[0]}.raw.jsonl")
        try:
            with open(measurement_info.csv_file, file_write_mode, newline="") as csv_file:
                csv_writer = CsvWriter(csv_file, mode, write_header_row, self.config)

                # To avoid bugs in some lights, when set to low brightness initially
                # where they turn off again. And also bugs where lights will turn off
                # again, after they received two turn-off commands, followed by a single
                # turn on command, we set them to maximum brightness, twice here.
                # See issue #2598
                assert self.light_info is not None
                self._smart_reason = REASON_WARMUP
                self.interaction.phase("Preparing lights", reason=REASON_WARMUP)
                try:
                    set_light_to_maximum_brightness(
                        self.light_controller,
                        self.light_info,
                        mode,
                        checkpoint=self._checkpoint,
                        phase=self.interaction.phase,
                        send=self._change_light_state_with_retry,
                    )
                except ControllerError as error:
                    raise RunnerError(str(error)) from error

                _LOGGER.info(
                    "Start taking measurements for color mode: %s",
                    mode.value,
                )

                measured = self._measured_so_far(measurement_info)
                if smart_applies(mode, self.config):
                    if not measurement_info.variations:
                        self._append_smart_batch(
                            mode,
                            measured,
                            measurement_info.variations,
                            all_variations,
                            remaining_variations,
                        )
                    else:
                        batch = next_smart_batch(mode, measured, self.config, self.light_info)
                        self._smart_stage = batch.stage
                        self._smart_reason = batch.reason
                else:
                    self._smart_stage = None
                    self._smart_reason = _grid_reason(mode)
                    _LOGGER.info("%s", self._smart_reason)
                self._report_progress(mode, all_variations, remaining_variations)
                previous_variation = None
                for count, variation in enumerate(measurement_info.variations):
                    while True:
                        self._log_progress(mode, count, variation, all_variations, remaining_variations)
                        _LOGGER.info("Changing light to: %s", variation)
                        self._checkpoint()
                        variation_start_time = time.time()
                        self._change_light_with_retry(mode, variation)
                        self.wait(variation, previous_variation)

                        previous_variation = variation

                        try:
                            self._checkpoint()
                            self._begin_raw_samples(raw_sample_writer, mode, variation)
                            measurement_result = self.take_power_measurement(mode, variation_start_time)
                            self._raise_if_standby_like_on(variation, measurement_result.power)
                        except OutdatedMeasurementError as error:
                            try:
                                measurement_result = self._remeasure_after_outdated(
                                    mode,
                                    variation,
                                    error,
                                )
                            except OutdatedMeasurementError as leftover:
                                _LOGGER.warning(
                                    "Retrying this point after outdated reading: %s",
                                    leftover,
                                )
                                self._wait(self.config.sleep_time)
                                continue
                        except StandbyLikeOnError as error:
                            _LOGGER.warning("Discarding measurement: %s", error)
                            if self._skip_after_repeated_standby_like(variation):
                                break
                            continue
                        except ZeroReadingError as error:
                            self._record_zero_reading()
                            self._report_progress(mode, all_variations, remaining_variations, variation)
                            _LOGGER.warning("Discarding measurement: %s", error)
                            self._raise_for_repeated_zero_readings(error)
                            continue
                        except PowerMeterError as error:
                            _LOGGER.warning(
                                "Retrying this point after meter error: %s",
                                error,
                            )
                            self._wait(self.config.sleep_time)
                            continue
                        self.num_0_readings = 0
                        self._standby_like_count = 0
                        _LOGGER.info("Measured power: %.2f", measurement_result.power)
                        self._checkpoint()
                        csv_writer.write_measurement(variation, measurement_result.power)
                        self._finish_raw_samples(measurement_result.power)
                        voltages.extend(measurement_result.voltages)
                        remaining_variations.remove(variation)
                        if smart_applies(mode, self.config):
                            measured.append(MeasuredPoint(variation, measurement_result.power))
                            if count == len(measurement_info.variations) - 1:
                                self._append_smart_batch(
                                    mode,
                                    measured,
                                    measurement_info.variations,
                                    all_variations,
                                    remaining_variations,
                                )
                        self._report_progress(mode, all_variations, remaining_variations, variation)
                        break

                _LOGGER.info(
                    "Hooray! measurements finished. Exported CSV file %s",
                    measurement_info.csv_file,
                )
        finally:
            self.measure_util.on_accepted_reading = None
            self._raw_writer = None
            raw_sample_writer.close()

        if self._extend and self._remeasure_existing and measurement_info.seed_rows:
            newly = load_mode_rows(measurement_info.csv_file, mode)
            write_mode_csv(
                measurement_info.csv_file,
                mode,
                union_rows(measurement_info.seed_rows, newly, prefer_last=True),
                plan=self.plan.for_mode(mode).variations if self.plan is not None else None,
                gzip_output=False,
            )

        if self.gzip:
            self.gzip_csv(measurement_info.csv_file)
        return voltages

    def _last_composite_reading(self) -> CompositeReading | None:
        """The most recent primary+witness(es) reading, if the power meter is a composite one."""

        power_meter = getattr(self.measure_util, "power_meter", None)
        if isinstance(power_meter, CompositePowerMeter):
            return power_meter.last_reading
        return None

    def _begin_raw_samples(self, writer: RawSampleWriter, mode: LutMode, variation: Variation) -> None:
        self._raw_writer = writer
        self._raw_mode = mode
        self._raw_variation = variation
        self._raw_lines_this_point = 0
        self.measure_util.on_accepted_reading = self._on_accepted_reading

    def _on_accepted_reading(self, result: MeasurementResult) -> None:
        self._write_raw_sample(result.power)
        self._raw_lines_this_point += 1

    def _write_raw_sample(self, power: float) -> None:
        if self._raw_writer is None or self._raw_mode is None or self._raw_variation is None:
            return
        self._raw_writer.write(
            mode=self._raw_mode,
            variation=self._raw_variation,
            reading=self._last_composite_reading(),
            power=power,
            settle_seconds=self.last_settle_seconds,
            settle_hit_cap=self.last_settle_hit_cap,
        )

    def _finish_raw_samples(self, power: float) -> None:
        if self._raw_lines_this_point == 0:
            self._write_raw_sample(power)
        self.measure_util.on_accepted_reading = None

    def _get_csv_write_options(self, measurement_info: MeasurementRunInput) -> tuple[Literal["w", "a"], bool]:
        if not measurement_info.is_resuming:
            return "w", True

        _LOGGER.info("Resuming measurements")
        return "a", False

    def _log_progress(
        self,
        mode: LutMode,
        count: int,
        variation: Variation,
        all_variations: list[Variation],
        remaining_variations: list[Variation],
    ) -> None:
        if count % 10 != 0:
            return

        time_left = self.calculate_time_left(mode, all_variations, remaining_variations, variation)
        completed, total = self._progress_counts(all_variations, remaining_variations)
        progress_percentage = (completed / total) * 100 if total else 100
        _LOGGER.info("Progress: %d%%, Estimated time left: %s", progress_percentage, time_left)

    def _report_progress(
        self,
        mode: LutMode,
        all_variations: list[Variation],
        remaining_variations: list[Variation],
        current_variation: Variation | None = None,
    ) -> None:
        completed_variations, total = self._progress_counts(all_variations, remaining_variations)
        self.interaction.progress(
            completed=completed_variations,
            total=total,
            phase=mode.value,
            remaining_seconds=self.calculate_time_left_seconds(
                mode,
                remaining_variations,
                current_variation,
            ),
            skipped=self.skipped_zero_readings,
            already_measured=self._already_measured_count,
        )
        if self._smart_stage:
            self.interaction.phase(self._smart_stage, reason=self._smart_reason)
        elif self._smart_reason:
            self.interaction.phase(mode.value, reason=self._smart_reason)

    def _progress_counts(
        self,
        all_variations: list[Variation],
        remaining_variations: list[Variation],
    ) -> tuple[int, int]:
        already = self._already_measured_count
        extra = self._smart_uninvented(all_variations, remaining_variations)
        total = already + len(all_variations) + extra
        completed = already + len(all_variations) - len(remaining_variations)
        return completed, total

    def _smart_uninvented(
        self,
        all_variations: list[Variation],
        remaining_variations: list[Variation],
    ) -> int:
        if self.plan is None or self.light_info is None:
            return 0
        return smart_uninvented_count(
            [mode_plan.mode for mode_plan in self.plan.modes],
            all_variations,
            remaining_variations,
            self.config,
            self.light_info,
            measured_by_mode=self._seed_points_by_mode,
        )

    def _observed_seconds_per_point(self, completed: int) -> float | None:
        if self._session_started_at is None:
            return None
        measured_here = completed - self._already_measured_count
        if measured_here < 5:
            return None
        return (time.time() - self._session_started_at) / measured_here

    def _measured_so_far(self, measurement_info: MeasurementRunInput) -> list[MeasuredPoint]:
        points: list[MeasuredPoint] = []
        seen: set[Variation] = set()
        for variation, row in measurement_info.seed_rows.items():
            points.append(MeasuredPoint(variation=variation, watt=row.watt))
            seen.add(variation)
        if os.path.exists(measurement_info.csv_file):
            for variation, row in load_mode_rows(measurement_info.csv_file, measurement_info.mode).items():
                if variation not in seen:
                    points.append(MeasuredPoint(variation=variation, watt=row.watt))
                    seen.add(variation)
        return points

    def _append_smart_batch(
        self,
        mode: LutMode,
        measured: list[MeasuredPoint],
        variations: list[Variation],
        all_variations: list[Variation],
        remaining_variations: list[Variation],
    ) -> None:
        assert self.light_info is not None
        batch = next_smart_batch(mode, measured, self.config, self.light_info)
        measured_keys = {item.variation for item in measured}
        extra = [point for point in batch.variations if point not in variations and point not in measured_keys]
        if not extra:
            return
        self._smart_stage = batch.stage
        self._smart_reason = batch.reason
        variations.extend(extra)
        remaining_variations.extend(extra)
        if self.active_plan is not None:
            all_variations[:] = self.active_plan.variations
        else:
            all_variations.extend(extra)
        _LOGGER.info(
            "Smart %s: appended %d variations (%s). %s",
            mode.value,
            len(extra),
            batch.stage,
            batch.reason,
        )

    def _record_zero_reading(self) -> None:
        self.num_0_readings += 1
        self.skipped_zero_readings += 1

    def _raise_for_repeated_zero_readings(self, error: ZeroReadingError) -> None:
        if self.num_0_readings >= MAX_CONSECUTIVE_ZERO_READINGS:
            raise RunnerError(ZERO_READING_ABORT_MESSAGE) from error

    def _skip_after_repeated_standby_like(self, variation: Variation) -> bool:
        """Retry a standby-like on-sample; after several, skip that point — do not abort the run."""

        self._standby_like_count += 1
        if self._standby_like_count < MAX_CONSECUTIVE_ZERO_READINGS:
            return False
        _LOGGER.warning(
            "Skipping %s after %d on-readings closer to standby than to the lowest on-load",
            variation,
            self._standby_like_count,
        )
        self._standby_like_count = 0
        return True

    def _settle(self, *, min_power: float = 0.0) -> None:
        """Wait for the light's power draw to stabilize after a change, before reading it.

        `sleep_time` is a fixed wait by default (`settle_tolerance_pct == 0`, opt-in
        required since not every power meter refreshes fast enough for polling to make
        sense). When enabled, `sleep_time` instead becomes an upper bound: the run polls
        the meter and proceeds as soon as the trailing window is settled: watched
        for `settle_window_seconds`, enough samples in that window, spread within
        the percentage *or* `settle_tolerance_w` (so a 0.1 W meter can settle at
        low load). Falls back to the full fixed wait if it never stabilizes --
        e.g. a meter that doesn't support polling faster than its own reporting
        interval, or a light whose driver genuinely never settles.

        `settle_min_wait` runs first (when settle is on): HA often reports the new
        brightness while power is still the previous plateau, and that leftover
        is already flat.
        """
        if self.config.settle_tolerance_pct <= 0:
            self.last_settle_seconds = None
            self.last_settle_hit_cap = None
            self._wait(self.config.sleep_time)
            # Plateau detection is opt-in (see the class docstring); without it there's no
            # settle outcome to report, but the label must still be set to something
            # current -- leaving it untouched would show whatever a *previous* variation's
            # settle wait left behind, stale and misleading.
            power_meter = getattr(self.measure_util, "power_meter", None)
            if hasattr(power_meter, "log_context"):
                power_meter.log_context = "settled"  # type: ignore[union-attr]
            return
        min_wait = self.config.settle_min_wait
        if min_wait > 0:
            _LOGGER.info("Waiting %.1fs after the light command before sampling power", min_wait)
            self._wait(min_wait)
        elapsed = self.measure_util.wait_for_plateau(
            self.config.sleep_time,
            tolerance_pct=self.config.settle_tolerance_pct,
            tolerance_w=self.config.settle_tolerance_w,
            window_seconds=self.config.settle_window_seconds,
            poll_interval=self.config.settle_poll_interval_seconds,
            min_power=min_power,
        )
        self.last_settle_seconds = min_wait + elapsed
        # A small epsilon since wait_for_plateau's own loop can return a hair under the
        # cap (it stops polling once elapsed >= max_wait, not exactly at it).
        self.last_settle_hit_cap = elapsed >= self.config.sleep_time - 0.05
        _LOGGER.debug("Settle wait finished after %.1fs (cap %.1fs)", elapsed, self.config.sleep_time)
        # Label the *accepted* reading that follows with why the wait ended, not just that
        # it did -- "settled" (the reading held flat) and "settle cap hit" (it never did,
        # and we gave up at the configured limit) are very different outcomes to see in
        # the log, even though both currently produce a recorded sample either way.
        power_meter = getattr(self.measure_util, "power_meter", None)
        if hasattr(power_meter, "log_context"):
            power_meter.log_context = "settle cap hit" if self.last_settle_hit_cap else "settled"  # type: ignore[union-attr]

    def _change_light_state_with_retry(self, mode: LutMode, on: bool = True, **kwargs: Any) -> None:
        """Keep asking until Home Assistant accepts the change.

        A dropped socket, a Matter peer timeout, or a slow ACK can all clear on
        their own. Aborting the session there throws away hours of samples for a
        light that is still in HA. Stop remains the way out if the lights are
        actually gone.
        """

        targets = _light_targets(self.light_controller)
        attempt = 0
        while True:
            attempt += 1
            try:
                self._checkpoint()
                _LOGGER.info(
                    "Changing %s: mode=%s %s (attempt %d)",
                    targets,
                    mode.value,
                    kwargs,
                    attempt,
                )
                self.light_controller.change_light_state(mode, on=on, **kwargs)
                return
            except ControllerError as error:
                detail = explain_home_assistant_error(error) if isinstance(error, ApiConnectionError) else str(error)
                _LOGGER.warning(
                    "Failed to change %s to %s: %s Waiting %.0fs then retrying (attempt %d).",
                    targets,
                    kwargs,
                    detail,
                    _LIGHT_CHANGE_RETRY_WAIT,
                    attempt,
                )
                self.interaction.notify(
                    f"Failed to change {targets}: {detail} Waiting {_LIGHT_CHANGE_RETRY_WAIT:.0f}s then retrying.",
                    warning=True,
                )
                self.interaction.phase(
                    "Waiting for lights to accept the change",
                    wait_seconds=_LIGHT_CHANGE_RETRY_WAIT,
                )
                self._wait(_LIGHT_CHANGE_RETRY_WAIT)

    def _change_light_with_retry(self, mode: LutMode, variation: Variation) -> None:
        self._change_light_state_with_retry(mode, on=True, **asdict(variation))
        self.interaction.operating_point(self._operating_point(mode, variation))

    def wait(self, variation: Variation, previous_variation: Variation | None) -> None:
        """Wait for the light to process the change"""
        if not previous_variation and self.config.sleep_time > 0:
            wait_seconds = self.config.sleep_time
            if self.config.settle_tolerance_pct > 0:
                wait_seconds += self.config.settle_min_wait
            self.interaction.phase(
                "Waiting for power to settle",
                wait_seconds=wait_seconds,
            )
        self._settle(min_power=0.05 if variation.bri > 0 else 0.0)

        # Extra fixed sleeps exist for cartesian runs without plateau detection:
        # sleep_initial after the first point (so a plug cannot report leftover
        # max-load), and hue/sat/CT wrap sleeps after a long jump. Settle already
        # waited for a power plateau, so another 5-10s is just idle.
        if self.config.settle_tolerance_pct > 0:
            return

        if not previous_variation:
            if self.config.sleep_initial > 0:
                _LOGGER.info("Waiting %d seconds...", self.config.sleep_initial)
                self.interaction.phase(
                    "Stabilizing light before the first reading",
                    wait_seconds=self.config.sleep_initial,
                )
                self._wait(self.config.sleep_initial)
            return

        if (
            isinstance(variation, ColorTempVariation)
            and isinstance(previous_variation, ColorTempVariation)
            and variation.ct < previous_variation.ct
        ):
            _LOGGER.info("Extra waiting for significant CT change...")
            self._wait(self.config.sleep_time_ct)
            return

        if isinstance(variation, HsVariation) and isinstance(previous_variation, HsVariation):
            if variation.hue < previous_variation.hue:
                _LOGGER.info("Extra waiting for significant HUE change...")
                self._wait(self.config.sleep_time_hue)
            if variation.sat < previous_variation.sat:
                _LOGGER.info("Extra waiting for significant SAT change...")
                self._wait(self.config.sleep_time_sat)
            return

        if (
            isinstance(variation, EffectVariation)
            and isinstance(previous_variation, EffectVariation)
            and variation.is_effect_changed(previous_variation)
        ):
            _LOGGER.info("Extra waiting for effect change...")
            self._wait(self.config.sleep_time_effect_change)

    def calculate_time_left(
        self,
        current_mode: LutMode,
        all_variations: list[Variation],
        remaining_variations: list[Variation],
        current_variation: Variation | None = None,
    ) -> str:
        """Try to guess the remaining time left. This will not account for measuring errors / retries obviously"""
        return self.format_time_left(
            self.calculate_time_left_seconds(
                current_mode,
                all_variations,
                remaining_variations,
                current_variation,
            ),
        )

    def calculate_time_left_seconds(
        self,
        current_mode: LutMode,
        all_variations: list[Variation],
        remaining_variations: list[Variation],
        current_variation: Variation | None = None,
    ) -> float:
        """Return the shared remaining-time estimate for progress consumers."""
        assert self.active_plan is not None
        assert all_variations == self.active_plan.variations
        completed, _total = self._progress_counts(all_variations, remaining_variations)
        return estimate_light_time_left(
            self.active_plan,
            self.config,
            current_mode=current_mode,
            remaining_variations=remaining_variations,
            current_variation=current_variation,
            extra_variations=self._smart_uninvented(all_variations, remaining_variations),
            seconds_per_point=self._observed_seconds_per_point(completed),
        )

    @staticmethod
    def format_time_left(time_left: float) -> str:
        """Format the time left in a human readable format"""
        if time_left < 0:
            time_left = 0
        if time_left > 3600:
            formatted_time = f"{round(time_left / 3600, 1)}h"
        elif time_left > 60:
            formatted_time = f"{round(time_left / 60, 1)}m"
        else:
            formatted_time = f"{round(time_left, 1)}s"

        return formatted_time

    def _remeasure_after_outdated(
        self,
        mode: LutMode,
        variation: Variation,
        cause: OutdatedMeasurementError,
    ) -> MeasurementResult:
        """Retry via light-nudge only when configured; otherwise keep the meter's reason."""
        return self.nudge_and_remeasure(mode, variation, cause)

    def nudge_and_remeasure(
        self,
        mode: LutMode,
        variation: Variation,
        cause: OutdatedMeasurementError | None = None,
    ) -> MeasurementResult:
        if self.config.max_nudges == 0:
            if cause is not None:
                raise cause
            raise OutdatedMeasurementError(
                "Power measurement is outdated and nudging is disabled (max_nudges=0)",
            )
        last_error = cause
        for _ in range(self.config.max_nudges):
            try:
                # Likely not significant enough change for PM to detect. Try nudging it
                _LOGGER.warning("Measurement is stuck, Nudging")
                # If brightness is low, set brightness high. Else, turn light off
                self._checkpoint()
                self.light_controller.change_light_state(
                    LutMode.BRIGHTNESS,
                    on=(variation.bri < 128),
                    bri=255,
                )
                self._wait(self.config.pulse_time_nudge)
                variation_start_time = time.time()
                self._checkpoint()
                self.light_controller.change_light_state(
                    mode,
                    on=True,
                    **asdict(variation),
                )
                self.interaction.operating_point(self._operating_point(mode, variation))
                # Wait a longer amount of time for the PM to settle
                self._wait(self.config.sleep_time_nudge)
                result = self.take_power_measurement(mode, variation_start_time)
                self.num_0_readings = 0
                return result
            except OutdatedMeasurementError as error:
                last_error = error
                continue
            except ZeroReadingError as error:
                self._record_zero_reading()
                _LOGGER.warning("Discarding measurement: %s", error)
                self._raise_for_repeated_zero_readings(error)
                continue
        detail = f" ({last_error})" if last_error is not None else ""
        raise OutdatedMeasurementError(
            f"Power measurement is outdated. Aborting after {self.config.max_nudges} nudge attempts{detail}",
        ) from last_error

    def should_resume(self, csv_file_path: str) -> bool:
        """Apply the configured resume policy to a non-empty measurement CSV."""
        if not os.path.exists(csv_file_path):
            return False

        size = os.path.getsize(csv_file_path)
        if size == 0:
            return False

        with open(csv_file_path) as csv_file:
            rows = csv.reader(csv_file)
            if len(list(rows)) == 1:
                return False

        should_resume = self._resume
        if should_resume:
            if not self.config.prompt_resume:
                return True
            return self.interaction.choose(
                f"CSV File {csv_file_path} already exists. Do you want to resume measurements?",
                default=True,
            )
        return should_resume

    def get_resume_variation(self, csv_file_path: str, mode: LutMode) -> Variation | None:
        """Parse the last complete CSV row into the variation from which the mode resumes.

        Trailing rows that cannot be parsed (typically a torn final line after a crash)
        are dropped from the file so appended measurements produce a valid CSV.
        """

        with open(csv_file_path, newline="") as csv_file:
            rows = list(csv.reader(csv_file))

        valid_row_count = len(rows)
        while valid_row_count > 1 and variation_from_csv_row(rows[valid_row_count - 1], mode) is None:
            valid_row_count -= 1

        if valid_row_count < len(rows):
            _LOGGER.warning(
                "Dropping %d incomplete trailing row(s) from %s before resuming",
                len(rows) - valid_row_count,
                csv_file_path,
            )
            with open(csv_file_path, "w", newline="") as csv_file:
                csv.writer(csv_file).writerows(rows[:valid_row_count])

        if valid_row_count == 1:
            return None
        return variation_from_csv_row(rows[valid_row_count - 1], mode)

    def take_power_measurement(
        self,
        mode: LutMode,
        start_timestamp: float,
        retry_count: int = 0,
    ) -> MeasurementResult:
        """Take an effect average or a timestamp-validated point reading."""
        if mode == LutMode.EFFECT:
            result = self.measure_util.take_average_measurement(
                self.config.measure_time_effect,
                convergence=AverageMeasurementConvergence(
                    min_duration=self.config.measure_time_effect_min,
                    window_duration=self.config.measure_time_effect_convergence_window,
                    absolute_threshold=self.config.measure_time_effect_convergence_abs,
                    relative_threshold=self.config.measure_time_effect_convergence_rel,
                ),
            )
        else:
            result = self.measure_util.take_measurement(start_timestamp, retry_count)

        # Determine per load power consumption
        power = result.power / self.num_lights

        return MeasurementResult(power=round(power, 2), voltages=result.voltages)

    @staticmethod
    def gzip_csv(csv_file_path: str) -> None:
        """Gzip the CSV file"""
        with (
            open(csv_file_path, "rb") as csv_file,
            gzip.open(
                f"{csv_file_path}.gz",
                "wb",
            ) as gzip_file,
        ):
            shutil.copyfileobj(csv_file, gzip_file)

    def measure_standby_power(self) -> MeasurementResult:
        """Return the once-recorded standby. Does not take a new reading."""

        if self._on_off_bounds is not None:
            return MeasurementResult(power=self._on_off_bounds.standby, voltages=self._standby_voltages)
        if self._export_directory:
            persisted = load_on_off_bounds(self._export_directory)
            if persisted is not None:
                return MeasurementResult(power=persisted.standby, voltages=self._standby_voltages)
        return MeasurementResult(power=0, voltages=[])

    def _read_standby_power(self) -> MeasurementResult:
        """The one lights-off watt reading, same settle/OCR/retry rules as a LUT point."""
        self._checkpoint()
        start_time = time.time()
        self._change_light_state_with_retry(LutMode.BRIGHTNESS, on=False)
        self.interaction.operating_point(LightOperatingPoint(type="light", on=False))
        self._settle(min_power=0.0)
        if self.config.sleep_standby > 0:
            _LOGGER.info(
                "Measuring standby power. Waiting for %d seconds...",
                self.config.sleep_standby,
            )
            self.interaction.phase(
                "Measuring standby power",
                wait_seconds=self.config.sleep_standby,
            )
            self._wait(self.config.sleep_standby)
        try:
            self._checkpoint()
            return self.take_power_measurement(LutMode.BRIGHTNESS, start_time)
        except OutdatedMeasurementError as error:
            return self._remeasure_after_outdated(LutMode.BRIGHTNESS, Variation(0), error)
        except ZeroReadingError:
            _LOGGER.error(
                "Measured 0 watt as standby usage, continuing now, "
                "but you probably need to have a look into measuring multiple lights at the same time "
                "or using a dummy load.",
            )
            return MeasurementResult(power=0, voltages=[])

    @staticmethod
    def _operating_point(mode: LutMode, variation: Variation) -> LightOperatingPoint:
        point = LightOperatingPoint(type="light", on=True, brightness=variation.bri)
        if mode == LutMode.COLOR_TEMP and isinstance(variation, ColorTempVariation):
            point["color_temp_mired"] = variation.ct
        elif mode == LutMode.HS and isinstance(variation, HsVariation):
            point["hue"] = variation.hue
            point["saturation"] = variation.sat
        elif mode == LutMode.EFFECT and isinstance(variation, EffectVariation):
            point["effect"] = variation.effect
        return point


def _grid_reason(mode: LutMode) -> str:
    if mode == LutMode.HS:
        return "Fixed HS grid from the hue, saturation, and brightness step settings — not chosen from measured power."
    if mode == LutMode.COLOR_TEMP:
        return (
            "Fixed color-temperature grid from the mired and brightness step settings — not chosen from measured power."
        )
    if mode == LutMode.BRIGHTNESS:
        return "Fixed brightness sweep from bri_bri_steps — not chosen from measured power."
    if mode == LutMode.EFFECT:
        return "Fixed effect list at each brightness step — not chosen from measured power."
    return "Fixed LUT grid from the step/division settings — not chosen from measured power."


def _light_targets(controller: LightController) -> str:
    entity_ids = getattr(controller, "entity_ids", None)
    if entity_ids:
        return ", ".join(str(item) for item in entity_ids)
    entity_id = getattr(controller, "entity_id", None)
    if entity_id:
        return str(entity_id)
    return type(controller).__name__


@dataclass(frozen=True)
class MeasurementRunInput:
    mode: LutMode
    csv_file: str
    variations: list[Variation]
    is_resuming: bool
    seed_rows: dict[Variation, LutRow] = field(default_factory=dict)


def _should_establish_on_off_bounds(request: LightMeasurementRequest) -> bool:
    """Dummy benches have no physical off/on gap; skip so tests and dry-runs stay unchanged."""

    return not request.controller.is_dummy and not isinstance(request.power_meter, DummyPowerMeterSpec)


class CsvWriter:
    def __init__(
        self,
        csv_file: TextIO,
        mode: LutMode,
        add_header: bool,
        parameters: MeasurementParameters,
    ) -> None:
        self.csv_file = csv_file
        self.config = parameters
        self.writer = csv.writer(csv_file)
        self.rows_written = 0
        if add_header:
            header_row = [*CSV_HEADERS[mode]]
            if self.config.csv_add_datetime_column:
                header_row.append("time")
            self.writer.writerow(header_row)

    def write_measurement(self, variation: Variation, power: float) -> None:
        """Write row with measurement to the CSV"""
        row = variation.to_csv_row()
        row.append(power)
        if self.config.csv_add_datetime_column:
            row.append(dt.now().strftime("%Y%m%d%H%M%S"))
        self.writer.writerow(row)
        self.rows_written += 1
        # Flush every row, not just every 50th: the CSV is now read mid-run by the live
        # "profile so far" plot (see api.py's /plots endpoint), so a buffered-but-unflushed
        # row is invisible to that reader even though the measurement itself has already
        # completed and moved on. Confirmed 2026-09-06: 22 variations measured, only the
        # first showed up on the live plot, because 22 % 50 never crossed a flush boundary.
        # A per-point wait of at least a fraction of a second (settle time, sampling, the
        # light API round-trip) makes the flush() cost irrelevant next to everything else
        # already happening on that same point.
        self.csv_file.flush()
