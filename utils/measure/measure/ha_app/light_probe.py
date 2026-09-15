from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
import json
import logging
from threading import RLock
import time
from typing import Literal

from measure.assembler import MeasurementAssembler
from measure.controller.light.capabilities import mired_to_kelvin
from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightController
from measure.execution import ImmediateInteraction
from measure.home_assistant import HomeAssistantManager
from measure.powermeter.errors import ZeroReadingError
from measure.powermeter.powermeter import PowerMeter
from measure.powermeter.spec import DummyPowerMeterSpec, PowerMeterSpec
from measure.request import LightMeasurementRequest, MeasurementRequest, ResumePolicy
from measure.runner.light_plan import Variation, build_light_plan, low_load_probe_variations
from measure.runner.on_off_bounds import on_load_distinguishable_from_standby
from measure.util.measure_util import MeasureUtil

LIGHT_LOAD_PROBE_CACHE_SECONDS = 600
LOW_POWER_MEASUREMENT_GUIDE_URL = "https://docs.powercalc.nl/contributing/measure/low-power-measurements/"
STANDBY_STEP_ID = "standby"
_LOGGER = logging.getLogger("measure")


@dataclass(frozen=True)
class LightLoadProbePoint:
    label: str
    mode: LutMode
    power_w: float


@dataclass(frozen=True)
class LightLoadProbeResult:
    checked_variations: int
    minimum_aggregate_power_w: float
    points: tuple[LightLoadProbePoint, ...]
    standby_aggregate_power_w: float | None = None


@dataclass(frozen=True)
class LightLoadProbeStep:
    """One off or on-point the client measures in its own HTTP request."""

    id: str
    kind: Literal["standby", "on"]
    label: str
    mode: LutMode | None = None


@dataclass(frozen=True)
class LightLoadProbeReading:
    id: str
    kind: Literal["standby", "on"]
    label: str
    power_w: float
    mode: LutMode | None = None


class LightLoadProbeError(Exception):
    """Raised when active preflight cannot verify the selected light's lowest loads.

    Only a genuine low-load failure carries documentation metadata; an adapter or
    connectivity failure must not be attributed to an unmeasurably low load.
    """

    def __init__(self, message: str, *, help_url: str | None = None, help_label: str | None = None) -> None:
        super().__init__(message)
        self.help_url = help_url
        self.help_label = help_label


def light_load_probe_applies(request: MeasurementRequest) -> bool:
    """True when a new light run should measure standby and low-load points first."""

    return (
        isinstance(request, LightMeasurementRequest)
        and request.resume_policy == ResumePolicy.NEW
        and request.dummy_load is None
        and not request.controller.is_dummy
        and not isinstance(request.power_meter, DummyPowerMeterSpec)
        and bool(request.modes - {LutMode.EFFECT})
    )


def complete_probe_result(
    standby_w: float,
    points: Sequence[LightLoadProbePoint],
) -> LightLoadProbeResult:
    """Assemble per-request readings and reject a floor that cannot tell on from off."""

    if not points:
        return LightLoadProbeResult(
            checked_variations=0,
            minimum_aggregate_power_w=0,
            points=(),
            standby_aggregate_power_w=standby_w,
        )
    minimum_on = min(point.power_w for point in points)
    if not on_load_distinguishable_from_standby(minimum_on, standby_w):
        raise LightLoadProbeError(
            f"The lowest on-load reading ({minimum_on:.3f} W) is too close to "
            f"standby ({standby_w:.3f} W). The lights are probably still off, or "
            "the meter cannot tell on from off. Check that every lamp actually "
            "lit, then retry.",
            help_url=LOW_POWER_MEASUREMENT_GUIDE_URL,
            help_label="Low-power measurement guide",
        )
    return LightLoadProbeResult(
        checked_variations=len(points),
        minimum_aggregate_power_w=minimum_on,
        points=tuple(points),
        standby_aggregate_power_w=standby_w,
    )


class LightLoadProbe:
    """Drive representative low-load light points before a long measurement starts.

    Each HTTP call measures one step with the same settle / OCR / ``sleep_standby``
    rules as a LUT point. The client sequences those calls so ingress never holds a
    multi-point wait.
    """

    def __init__(
        self,
        build_assembler: Callable[[], MeasurementAssembler],
        *,
        build_power_meter: Callable[[PowerMeterSpec], PowerMeter] | None = None,
        wait: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._build_assembler = build_assembler
        self._build_power_meter = build_power_meter
        self._wait = wait
        self._monotonic = monotonic
        self._now = now
        self._cache: dict[str, tuple[float, LightLoadProbeResult]] = {}
        self._lock = RLock()

    def plan(self, request: LightMeasurementRequest) -> tuple[LightLoadProbeStep, ...]:
        assembler = self._build_assembler()
        controller: LightController | None = None
        try:
            controller = assembler.build_light_controller(request.controller)
            variations = self._variations(controller, request)
            return _steps_for(variations)
        except LightLoadProbeError:
            raise
        except Exception as error:
            raise LightLoadProbeError(f"Could not plan the active light check: {error}") from error
        finally:
            if controller is not None:
                try:
                    controller.close()
                except Exception as error:  # noqa: BLE001 - planning must still return
                    _LOGGER.warning("Could not close the light controller after planning the light check: %s", error)

    def measure_step(self, request: LightMeasurementRequest, step_id: str) -> LightLoadProbeReading:
        """Measure one standby or on-point. Safe to call from its own HTTP request."""

        assembler = self._build_assembler()
        controller: LightController | None = None
        meter: PowerMeter | None = None
        light_driven = False
        try:
            controller = assembler.build_light_controller(request.controller)
            variations = self._variations(controller, request)
            steps = {step.id: step for step in _steps_for(variations)}
            step = steps.get(step_id)
            if step is None:
                raise LightLoadProbeError(f"Unknown light-check step {step_id!r}")
            build_meter = self._build_power_meter or assembler.build_power_meter
            meter = build_meter(request.power_meter)
            measure_util = MeasureUtil(meter, request.parameters, wait=self._wait)
            light_driven = True
            start_timestamp = self._now()
            if step.kind == "standby":
                controller.change_light_state(LutMode.BRIGHTNESS, on=False)
                power = self._measure_power(
                    measure_util,
                    request,
                    start_timestamp=start_timestamp,
                    standby=True,
                    initial=False,
                )
            else:
                variation = variations[int(step.id)]
                controller.change_light_state(variation.mode, on=True, **asdict(variation))
                power = self._measure_power(
                    measure_util,
                    request,
                    start_timestamp=start_timestamp,
                    standby=False,
                    initial=step.id == "0",
                )
            return LightLoadProbeReading(
                id=step.id,
                kind=step.kind,
                label=step.label,
                power_w=round(power, 3),
                mode=step.mode,
            )
        except LightLoadProbeError:
            raise
        except ZeroReadingError as error:
            raise _zero_reading_error() from error
        except Exception as error:
            raise LightLoadProbeError(f"Could not complete the active light check: {error}") from error
        finally:
            self._cleanup(controller, meter, light_driven=light_driven)

    def evaluate(self, request: LightMeasurementRequest) -> LightLoadProbeResult:
        key = self._cache_key(request)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and self._monotonic() - cached[0] < LIGHT_LOAD_PROBE_CACHE_SECONDS:
                return cached[1]

        result = self._probe(request)
        with self._lock:
            self._cache[key] = (self._monotonic(), result)
        return result

    def _probe(self, request: LightMeasurementRequest) -> LightLoadProbeResult:
        assembler = self._build_assembler()
        controller: LightController | None = None
        meter: PowerMeter | None = None
        light_driven = False
        try:
            controller = assembler.build_light_controller(request.controller)
            variations = self._variations(controller, request)
            if not variations:
                return LightLoadProbeResult(checked_variations=0, minimum_aggregate_power_w=0, points=())

            build_meter = self._build_power_meter or assembler.build_power_meter
            meter = build_meter(request.power_meter)
            measure_util = MeasureUtil(meter, request.parameters, wait=self._wait)
            light_driven = True
            controller.change_light_state(LutMode.BRIGHTNESS, on=False)
            standby = round(
                self._measure_power(measure_util, request, standby=True, initial=False),
                3,
            )
            points = [
                LightLoadProbePoint(
                    label=light_load_probe_label(variation),
                    mode=variation.mode,
                    power_w=round(
                        self._measure_variation(
                            controller,
                            measure_util,
                            request,
                            variation,
                            initial=index == 0,
                        ),
                        3,
                    ),
                )
                for index, variation in enumerate(variations)
            ]
            return complete_probe_result(standby, points)
        except LightLoadProbeError:
            raise
        except ZeroReadingError as error:
            raise _zero_reading_error() from error
        except Exception as error:
            raise LightLoadProbeError(f"Could not complete the active light check: {error}") from error
        finally:
            self._cleanup(controller, meter, light_driven=light_driven)

    def _variations(self, controller: LightController, request: LightMeasurementRequest) -> list[Variation]:
        light_info = controller.get_light_info()
        effects = controller.get_effect_list() if LutMode.EFFECT in request.modes else []
        plan = build_light_plan(request.modes, request.parameters, light_info, effects)
        return low_load_probe_variations(plan)

    def _measure_variation(
        self,
        controller: LightController,
        measure_util: MeasureUtil,
        request: LightMeasurementRequest,
        variation: Variation,
        *,
        initial: bool,
    ) -> float:
        start_timestamp = self._now()
        controller.change_light_state(variation.mode, on=True, **asdict(variation))
        return self._measure_power(
            measure_util,
            request,
            initial=initial,
            standby=False,
            start_timestamp=start_timestamp,
        )

    def _measure_power(
        self,
        measure_util: MeasureUtil,
        request: LightMeasurementRequest,
        *,
        initial: bool,
        standby: bool,
        start_timestamp: float | None = None,
    ) -> float:
        """Same settle / extra-wait / OCR-freshness rules as a LUT point."""

        started = start_timestamp if start_timestamp is not None else self._now()
        parameters = request.parameters
        if parameters.settle_tolerance_pct > 0:
            if parameters.settle_min_wait > 0:
                self._wait(parameters.settle_min_wait)
            measure_util.wait_for_plateau(
                parameters.sleep_time,
                tolerance_pct=parameters.settle_tolerance_pct,
                tolerance_w=parameters.settle_tolerance_w,
                window_seconds=parameters.settle_window_seconds,
                poll_interval=parameters.settle_poll_interval_seconds,
                min_power=0.0 if standby else 0.05,
            )
        else:
            self._wait(parameters.sleep_time)
        extra = parameters.sleep_standby if standby else (parameters.sleep_initial if initial else 0.0)
        if extra > 0:
            self._wait(extra)
        return measure_util.take_measurement(start_timestamp=started).power

    def _cleanup(
        self,
        controller: LightController | None,
        meter: PowerMeter | None,
        *,
        light_driven: bool,
    ) -> None:
        if meter is not None:
            try:
                meter.close()
            except Exception as error:  # noqa: BLE001 - cleanup must not mask the probe result
                _LOGGER.warning("Could not close the power meter after the active preflight check: %s", error)
        if controller is not None:
            if light_driven:
                try:
                    controller.change_light_state(LutMode.BRIGHTNESS, on=False)
                except Exception as error:  # noqa: BLE001 - cleanup must not mask the probe result
                    _LOGGER.warning("Could not turn off the light after the active preflight check: %s", error)
            try:
                controller.close()
            except Exception as error:  # noqa: BLE001 - cleanup must not mask the probe result
                _LOGGER.warning("Could not close the light controller after the active preflight check: %s", error)

    @staticmethod
    def _cache_key(request: LightMeasurementRequest) -> str:
        value = request.model_dump(mode="json")
        value["modes"] = sorted(value["modes"])
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _steps_for(variations: Sequence[Variation]) -> tuple[LightLoadProbeStep, ...]:
    steps = [LightLoadProbeStep(id=STANDBY_STEP_ID, kind="standby", label="Standby")]
    steps.extend(
        LightLoadProbeStep(
            id=str(index),
            kind="on",
            label=light_load_probe_label(variation),
            mode=variation.mode,
        )
        for index, variation in enumerate(variations)
    )
    return tuple(steps)


def _zero_reading_error() -> LightLoadProbeError:
    return LightLoadProbeError(
        "The power meter repeatedly returned 0 W while checking the selected light at its lowest-load "
        "settings. The measurement would likely fail later. Measure multiple identical lights together, "
        "use a suitable resistive dummy load, use a more sensitive meter, or increase Minimum brightness "
        "when that is acceptable for the profile.",
        help_url=LOW_POWER_MEASUREMENT_GUIDE_URL,
        help_label="Low-power measurement guide",
    )


def app_measurement_assembler(
    *,
    home_assistant: HomeAssistantManager,
    shelly_password: str | None,
) -> MeasurementAssembler:
    """Build the non-interactive adapter graph used by an app preflight probe."""

    return MeasurementAssembler(
        ImmediateInteraction(),
        home_assistant=home_assistant,
        shelly_password=shelly_password,
    )


def light_load_probe_label(variation: Variation) -> str:
    """Describe a probe variation in native values for the preflight review."""

    values = asdict(variation)
    if variation.mode == LutMode.COLOR_TEMP:
        return f"Color temperature {mired_to_kelvin(int(values['ct']))} K · brightness {variation.bri}"
    if variation.mode == LutMode.HS:
        hue_degrees = round(int(values["hue"]) / 65535 * 360)
        saturation_percent = round(int(values["sat"]) / 255 * 100)
        return f"Color {hue_degrees}° / {saturation_percent}% saturation · brightness {variation.bri}"
    return f"Brightness {variation.bri}"
