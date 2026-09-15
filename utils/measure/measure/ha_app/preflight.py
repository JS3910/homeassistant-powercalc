from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any, Protocol

from measure.const import DUMMY_LOAD_MEASUREMENT_COUNT, DUMMY_LOAD_MEASUREMENTS_DURATION
from measure.controller.charging.const import ATTR_BATTERY_LEVEL
from measure.controller.charging.spec import HassChargingControllerSpec, charging_entity_domain
from measure.controller.fan.spec import HassFanControllerSpec
from measure.controller.light.capabilities import common_effects, merge_light_infos
from measure.controller.light.const import MAX_MIRED, MIN_MIRED, LutMode
from measure.controller.light.controller import LightInfo
from measure.controller.light.dummy import DummyLightController
from measure.controller.light.spec import (
    DummyLightControllerSpec,
    HassLightControllerSpec,
    HassMultiLightControllerSpec,
    HueLightControllerSpec,
)
from measure.controller.media.spec import HassMediaControllerSpec
from measure.home_assistant_entities import DeviceClass, EntityDomain
from measure.powermeter.diagnostics import DiagnosticStatus, PowerMeterDiagnostic
from measure.powermeter.spec import (
    CompositePowerMeterSpec,
    DummyPowerMeterSpec,
    HassPowerMeterSpec,
    KasaPowerMeterSpec,
    MyStromPowerMeterSpec,
    OcrPowerMeterSpec,
    OwonOwh98xxPowerMeterSpec,
    PowerMeterSpec,
    ShellyPowerMeterSpec,
    TasmotaPowerMeterSpec,
    TuyaPowerMeterSpec,
)
from measure.request import (
    ChargingMeasurementRequest,
    DummyLoadCalibrationRequest,
    FanMeasurementRequest,
    LightMeasurementRequest,
    MeasurementRequest,
    RecorderMeasurementRequest,
    RecorderProfileRecipe,
    ResumePolicy,
    SpeakerMeasurementRequest,
)
from measure.runner.light_plan import (
    Variation,
    build_light_plan,
    estimate_light_run_seconds,
    estimate_light_time_left,
    modes_to_measure,
    summarize_light_modes,
)
from measure.runner.lut_csv import missing_variations
from measure.runner.smart_envelope import MeasuredPoint, estimate_smart_remaining, smart_applies

#: Every meter type the Home Assistant app's runtime can actually build and read from
#: (mirrors `SinglePowerMeterSpec` in `powermeter/spec.py` -- kept as an isinstance-able
#: tuple here since that's a typing.Annotated union, not a runtime-checkable class). A
#: `CompositePowerMeterSpec` is unwrapped to its primary before checking against this.
_SUPPORTED_SINGLE_METER_TYPES = (
    HassPowerMeterSpec,
    ShellyPowerMeterSpec,
    KasaPowerMeterSpec,
    MyStromPowerMeterSpec,
    TasmotaPowerMeterSpec,
    TuyaPowerMeterSpec,
    OwonOwh98xxPowerMeterSpec,
    OcrPowerMeterSpec,
)


class PreflightError(Exception):
    """Raised when a validated request cannot run against current external state."""


class ActiveSessionError(PreflightError):
    """Raised when a new measurement conflicts with the active session."""


class EntityRecord(Protocol):
    entity_id: str
    domain: str
    device_class: DeviceClass | None
    device_id: str | None
    state: str
    attribute_names: list[str]
    supported_modes: list[LutMode] | None
    effect_list: list[str] | None
    min_mired: int | None
    max_mired: int | None
    model_id: str | None
    member_entity_ids: list[str]


EntityLoader = Callable[[EntityDomain | None, DeviceClass | None], Sequence[EntityRecord]]
AllEntityLoader = Callable[[], Sequence[EntityRecord]]


@dataclass(frozen=True)
class PreflightResult:
    warnings: tuple[str, ...] = ()
    estimated_variations: int | None = None
    estimated_duration_seconds: int | None = None
    supported_modes: tuple[LutMode, ...] | None = None
    power_meter_diagnostic: PowerMeterDiagnostic | None = None
    battery_level_entity_id: str | None = None
    battery_level_attribute: str | None = None


@dataclass(frozen=True)
class LightModeEstimate:
    mode: LutMode
    axes: dict[str, int]
    points: int
    summary: str


@dataclass(frozen=True)
class LightPlanEstimate:
    """Cheap plan-only estimate for the setup page. No entity probe, no session lock."""

    modes: tuple[LightModeEstimate, ...]
    total_points: int
    total_readings: int | None
    max_duration_seconds: int
    used_default_range: bool
    remaining_points: int | None = None
    estimated_duration_seconds: int | None = None
    estimated_from_runs: int | None = None


@dataclass(frozen=True)
class HistoricalRunTiming:
    """Wall time and *new* point count from one earlier light run of a model + meter.

    ``completed_points`` is points taken in that run, not seed rows already on disk.
    """

    session_id: str
    model_id: str
    measure_device: str
    elapsed_seconds: float
    completed_points: int
    modes: frozenset[LutMode]
    fast_test_mode: bool = False


#: Fewer than this and the seconds-per-point rate is too noisy to show.
_MIN_HISTORICAL_POINTS = 5


def historical_seconds_per_point(
    request: LightMeasurementRequest,
    runs: Sequence[HistoricalRunTiming],
) -> tuple[float, int] | None:
    """Weighted seconds/point from prior runs of this model and meter.

    Effect points take minutes; mix them with HS/CT only when the current request
    also measures effects. The refine seed is always eligible — it is this DUT and
    meter by definition.
    """

    want_effect = LutMode.EFFECT in request.modes
    usable: list[HistoricalRunTiming] = []
    for run in runs:
        if run.fast_test_mode or run.completed_points < _MIN_HISTORICAL_POINTS or run.elapsed_seconds <= 0:
            continue
        if (LutMode.EFFECT in run.modes) != want_effect:
            continue
        is_seed = bool(request.seed_session_id) and request.seed_session_id == run.session_id
        same_combo = bool(request.model_id) and request.model_id == run.model_id
        same_combo = same_combo and bool(request.measure_device) and request.measure_device == run.measure_device
        if not is_seed and not same_combo:
            continue
        usable.append(run)
    if not usable:
        return None
    elapsed = sum(run.elapsed_seconds for run in usable)
    points = sum(run.completed_points for run in usable)
    if points <= 0 or elapsed <= 0:
        return None
    return elapsed / points, len(usable)


MODEL_UNCONFIRMED_WARNING = (
    "Could not confirm that every selected light has the same model. Verify this before starting."
)


@dataclass(frozen=True)
class LightSelection:
    """The lights one request drives, reduced to the capabilities they all share."""

    lights: tuple[EntityRecord, ...]
    supported_modes: set[LutMode]
    light_info: LightInfo
    effects: list[str]


def _no_group_member_overlap(selection: LightSelection, _: LightMeasurementRequest) -> tuple[str, ...]:
    """A group already drives its members, so selecting both would measure them twice."""

    members = {member for light in selection.lights for member in light.member_entity_ids}
    if members & {light.entity_id for light in selection.lights}:
        raise PreflightError("A light group and one of its members cannot both be selected")
    return ()


def _count_covers_selection(selection: LightSelection, request: LightMeasurementRequest) -> tuple[str, ...]:
    """Measured power is divided by the count, so it cannot describe fewer lights than are driven."""

    if request.multiple_light_count < len(selection.lights):
        raise PreflightError("Number of lights cannot be lower than the number of selected lights")
    from measure.home_assistant_entities import leaf_light_entity_ids

    leaves = leaf_light_entity_ids(
        [light.entity_id for light in selection.lights],
        {light.entity_id: light.member_entity_ids for light in selection.lights},
    )
    if leaves and request.multiple_light_count != len(leaves):
        return (
            f"Number of lights is {request.multiple_light_count} but the selected "
            f"group has {len(leaves)} member lights. Measured power is divided by the count.",
        )
    return ()


def _models_agree(selection: LightSelection, _: LightMeasurementRequest) -> tuple[str, ...]:
    """One profile is produced for all lights, so they must be the same model."""

    models = {light.model_id for light in selection.lights}
    if len(models - {None}) > 1:
        raise PreflightError("Selected lights must have the same model ID")
    if len(selection.lights) > 1 and None in models:
        return (MODEL_UNCONFIRMED_WARNING,)
    return ()


def _modes_supported(selection: LightSelection, request: LightMeasurementRequest) -> tuple[str, ...]:
    # Same set the runner will sweep. Implied brightness is not a requested LUT.
    planned = modes_to_measure(request.modes)
    missing = sorted(mode.value for mode in planned - selection.supported_modes)
    if missing:
        advertised = ", ".join(sorted(mode.value for mode in selection.supported_modes)) or "none"
        raise PreflightError(
            f"Selected light does not advertise {', '.join(missing)} "
            f"(advertises {advertised})",
        )
    return ()


def _color_temp_range_overlaps(selection: LightSelection, _: LightMeasurementRequest) -> tuple[str, ...]:
    if selection.light_info.min_mired > selection.light_info.max_mired:
        raise PreflightError("Selected lights do not share a color temperature range")
    return ()


LightRule = Callable[[LightSelection, LightMeasurementRequest], tuple[str, ...]]

#: Checks applied to a light selection, in order. Each returns warnings or raises a PreflightError,
#: so a new condition is added here rather than by growing the caller.
LIGHT_RULES: tuple[LightRule, ...] = (
    _no_group_member_overlap,
    _count_covers_selection,
    _models_agree,
    _modes_supported,
    _color_temp_range_overlaps,
)


def _light_info(light: EntityRecord) -> LightInfo:
    return LightInfo(
        "unknown",
        min_mired=light.min_mired if light.min_mired is not None else MIN_MIRED,
        max_mired=light.max_mired if light.max_mired is not None else MAX_MIRED,
    )


class MeasurementPreflight:
    """Validate a request against current session, storage and entity state."""

    def __init__(
        self,
        *,
        has_active_session: Callable[[], bool],
        verify_storage: Callable[[], None],
        load_entities: EntityLoader,
        load_all_entities: AllEntityLoader | None = None,
        diagnose_power_meter: Callable[[PowerMeterSpec], PowerMeterDiagnostic] | None = None,
        developer_mode: bool = False,
        load_measured_variations: Callable[[str], Mapping[LutMode, Collection[Variation]]] | None = None,
    ) -> None:
        self._has_active_session = has_active_session
        self._verify_storage = verify_storage
        self._load_entities = load_entities
        self._load_all_entities = load_all_entities
        self._diagnose_power_meter = diagnose_power_meter
        self._developer_mode = developer_mode
        self._load_measured_variations = load_measured_variations

    def validate(
        self,
        request: MeasurementRequest,
        *,
        skip_power_meter_diagnostic: bool = False,
    ) -> PreflightResult:
        """Return warnings and estimates, or raise a typed preflight error."""

        self._validate_adapters(request)
        if self._has_active_session():
            raise ActiveSessionError("A measurement session is already active")
        try:
            self._verify_storage()
        except OSError as error:
            raise PreflightError("Persistent app storage is not writable") from error

        self._validate_power_meter(request)
        diagnostic = self._diagnose_dummy_load(request)

        if isinstance(request, LightMeasurementRequest):
            result = self._validate_light(request)
        else:
            result = self._validate_controller(request)

        warnings = list(result.warnings)
        duration = result.estimated_duration_seconds
        if isinstance(request.dummy_load, DummyLoadCalibrationRequest):
            warnings.append(
                "Dummy-load calibration takes at least 10 minutes and repeats until the resistance is stable.",
            )
            duration = (duration or 0) + DUMMY_LOAD_MEASUREMENT_COUNT * DUMMY_LOAD_MEASUREMENTS_DURATION

        if not skip_power_meter_diagnostic:
            diagnostic = self._collect_power_meter_diagnostic(request, diagnostic, warnings)

        return PreflightResult(
            warnings=tuple(warnings),
            estimated_variations=result.estimated_variations,
            estimated_duration_seconds=duration,
            supported_modes=result.supported_modes,
            power_meter_diagnostic=diagnostic,
            battery_level_entity_id=result.battery_level_entity_id,
            battery_level_attribute=result.battery_level_attribute,
        )

    def _diagnose_dummy_load(self, request: MeasurementRequest) -> PowerMeterDiagnostic | None:
        """Diagnose the power meter upfront when a dummy load is used, so its voltage support can be validated."""
        if request.dummy_load is None or self._diagnose_power_meter is None:
            return None

        diagnostic = self._diagnose_power_meter(request.power_meter)
        self._validate_dummy_load_voltage(diagnostic)
        return diagnostic

    def _collect_power_meter_diagnostic(
        self,
        request: MeasurementRequest,
        diagnostic: PowerMeterDiagnostic | None,
        warnings: list[str],
    ) -> PowerMeterDiagnostic | None:
        """Diagnose the power meter when not done yet and translate the result into an error or warnings."""
        if self._diagnose_power_meter is None:
            return diagnostic

        if diagnostic is None:
            diagnostic = self._diagnose_power_meter(request.power_meter)
        if not diagnostic.success:
            raise PreflightError(diagnostic.message or "Could not read from the power meter")
        if diagnostic.status in {DiagnosticStatus.WARNING, DiagnosticStatus.POOR}:
            warnings.extend(diagnostic.messages)
        return diagnostic

    def _validate_adapters(self, request: MeasurementRequest) -> None:
        power_meter = request.power_meter
        if isinstance(power_meter, DummyPowerMeterSpec):
            if not self._developer_mode:
                raise PreflightError("Dummy power meters require developer mode in the Home Assistant app")
        else:
            primary = power_meter.primary if isinstance(power_meter, CompositePowerMeterSpec) else power_meter
            if not isinstance(primary, _SUPPORTED_SINGLE_METER_TYPES):
                label = primary.type.value.replace("_", " ").title()
                raise PreflightError(f"{label} power meters are not supported by the Home Assistant app")

        controller = request.controller
        if controller is None:
            return
        if controller.is_dummy:
            if not self._developer_mode:
                raise PreflightError("Dummy controllers require developer mode in the Home Assistant app")
            return
        if isinstance(
            controller,
            HassLightControllerSpec
            | HassMultiLightControllerSpec
            | HassMediaControllerSpec
            | HassChargingControllerSpec
            | HassFanControllerSpec,
        ):
            return
        if isinstance(controller, HueLightControllerSpec):
            raise PreflightError("Hue light controllers are not supported by the Home Assistant app")
        raise PreflightError(f"{type(controller).__name__} is not supported by the Home Assistant app")

    def _validate_power_meter(self, request: MeasurementRequest) -> None:
        power_meter = request.power_meter
        primary = power_meter.primary if isinstance(power_meter, CompositePowerMeterSpec) else power_meter
        if isinstance(primary, HassPowerMeterSpec):
            self._validate_hass_power_meter(primary, dummy_load=request.dummy_load is not None)
        if isinstance(power_meter, CompositePowerMeterSpec):
            for witness in power_meter.witnesses:
                if isinstance(witness.meter, HassPowerMeterSpec):
                    self._validate_hass_power_meter(witness.meter, dummy_load=False)

    def _validate_hass_power_meter(self, power_meter: HassPowerMeterSpec, *, dummy_load: bool) -> None:
        powers = {entity.entity_id for entity in self._load_entities(None, DeviceClass.POWER)}
        if power_meter.entity_id not in powers:
            raise PreflightError("Selected power entity is unavailable or not measured in W")
        if dummy_load and not power_meter.voltage_entity_id:
            raise PreflightError("A voltage sensor is required when using a resistive dummy load")
        if power_meter.voltage_entity_id:
            voltages = {entity.entity_id for entity in self._load_entities(None, DeviceClass.VOLTAGE)}
            if power_meter.voltage_entity_id not in voltages:
                raise PreflightError("Selected voltage entity is unavailable or not measured in V")

    @staticmethod
    def _validate_dummy_load_voltage(diagnostic: PowerMeterDiagnostic) -> None:
        if diagnostic.supports_voltage is False:
            raise PreflightError(
                "The selected power meter does not support voltage measurements required for dummy loads",
            )
        if diagnostic.supports_voltage is None:
            if not diagnostic.success:
                raise PreflightError(diagnostic.message or "Could not read from the power meter")
            raise PreflightError(
                "Could not determine whether the selected power meter supports voltage measurements "
                "required for dummy loads",
            )

    def _validate_controller(self, request: MeasurementRequest) -> PreflightResult:
        """Apply the check this request type declares; a type absent here drives nothing up front."""

        checks: dict[type[MeasurementRequest], Callable[[Any], PreflightResult]] = {
            SpeakerMeasurementRequest: self._validate_speaker,
            FanMeasurementRequest: self._validate_fan,
            ChargingMeasurementRequest: self._validate_charging,
            RecorderMeasurementRequest: self._validate_recorder,
        }
        check = checks.get(type(request))
        return PreflightResult() if check is None else check(request)

    def _validate_recorder(self, request: RecorderMeasurementRequest) -> PreflightResult:
        if not request.recorded_entity_ids:
            return PreflightResult()
        if self._load_all_entities is None:
            raise PreflightError("Home Assistant entity metadata is unavailable")

        all_entities = {entity.entity_id: entity for entity in self._load_all_entities()}
        if missing := [entity_id for entity_id in request.recorded_entity_ids if entity_id not in all_entities]:
            raise PreflightError(f"Selected recorder entity does not exist: {missing[0]}")

        if request.profile_recipe != RecorderProfileRecipe.VACUUM_ROBOT:
            return PreflightResult()

        vacuums = {entity.entity_id: entity for entity in self._load_entities(EntityDomain.VACUUM, None)}
        vacuum = vacuums.get(request.vacuum_entity_id or "")
        if vacuum is None:
            raise PreflightError("Selected vacuum is unavailable")

        batteries = {entity.entity_id: entity for entity in self._load_entities(None, DeviceClass.BATTERY)}
        battery = batteries.get(request.battery_entity_id or "")
        if battery is None:
            raise PreflightError("Selected battery sensor is unavailable or not a numeric percentage")
        if vacuum.device_id is None or battery.device_id != vacuum.device_id:
            raise PreflightError("Battery sensor must belong to the same Home Assistant device as the vacuum")
        return PreflightResult()

    def _validate_speaker(self, request: SpeakerMeasurementRequest) -> PreflightResult:
        if isinstance(request.controller, HassMediaControllerSpec):
            self._require_entity(
                request.controller.entity_id,
                EntityDomain.MEDIA_PLAYER,
                "Selected media player is unavailable",
            )
        return PreflightResult()

    def _validate_fan(self, request: FanMeasurementRequest) -> PreflightResult:
        if isinstance(request.controller, HassFanControllerSpec):
            self._require_entity(request.controller.entity_id, EntityDomain.FAN, "Selected fan is unavailable")
        return PreflightResult()

    def _validate_charging(self, request: ChargingMeasurementRequest) -> PreflightResult:
        if not isinstance(request.controller, HassChargingControllerSpec):
            return PreflightResult()
        domain = EntityDomain(charging_entity_domain(request.charging_device_type))
        if not request.controller.entity_id.startswith(f"{domain}."):
            raise PreflightError("Charging device type does not match the selected entity")
        charging_entity = self._require_entity(
            request.controller.entity_id,
            domain,
            "Selected charging device is unavailable",
        )
        return self._validate_charging_battery_source(charging_entity)

    def _validate_charging_battery_source(self, charging_entity: EntityRecord) -> PreflightResult:
        # Prefer a separate battery sensor on the same device (the modern HA default),
        # falling back to the battery_level attribute when none is available.
        battery_sensor = self._find_related_battery_sensor(charging_entity)
        if battery_sensor is not None:
            try:
                level = float(battery_sensor.state)
            except ValueError, TypeError:
                level = math.nan
            if not math.isfinite(level) or not 0 <= level <= 100:
                raise PreflightError("Battery level sensor must report a numeric percentage between 0 and 100")
            return PreflightResult(battery_level_entity_id=battery_sensor.entity_id)

        if ATTR_BATTERY_LEVEL in charging_entity.attribute_names:
            return PreflightResult(battery_level_attribute=ATTR_BATTERY_LEVEL)
        raise PreflightError(
            f"No battery level sensor was found on the same device, and attribute "
            f"{ATTR_BATTERY_LEVEL} is not available on the charging device",
        )

    def _find_related_battery_sensor(self, charging_entity: EntityRecord) -> EntityRecord | None:
        """Return a battery sensor on the same device as the charging entity, if any."""

        if charging_entity.device_id is None:
            return None
        return next(
            (
                sensor
                for sensor in self._load_entities(None, DeviceClass.BATTERY)
                if sensor.device_id == charging_entity.device_id
            ),
            None,
        )

    def _validate_light(self, request: LightMeasurementRequest) -> PreflightResult:
        if isinstance(request.controller, DummyLightControllerSpec):
            return self._estimate_dummy_light(request)
        if not isinstance(request.controller, HassLightControllerSpec | HassMultiLightControllerSpec):
            raise PreflightError("Selected light entity is unavailable")

        selection = self._resolve_lights(request.controller.entity_ids)
        warnings = tuple(warning for rule in LIGHT_RULES for warning in rule(selection, request))
        plan = build_light_plan(request.modes, request.parameters, selection.light_info, selection.effects)
        remaining = self._remaining_light_variations(request, plan.variations)
        return PreflightResult(
            warnings=warnings,
            estimated_variations=len(remaining),
            estimated_duration_seconds=round(
                estimate_light_time_left(plan, request.parameters, remaining_variations=remaining),
            ),
            supported_modes=tuple(sorted(selection.supported_modes, key=str)),
        )

    def _resolve_lights(self, entity_ids: Sequence[str]) -> LightSelection:
        """Look the selected lights up in the catalog and reduce them to their shared capabilities."""

        lights = {entity.entity_id: entity for entity in self._load_entities(EntityDomain.LIGHT, None)}
        selected = [lights[entity_id] for entity_id in entity_ids if entity_id in lights]
        if len(selected) != len(entity_ids):
            raise PreflightError("Selected light entity is unavailable")
        return LightSelection(
            lights=tuple(selected),
            supported_modes=set.intersection(*(set(light.supported_modes or []) for light in selected)),
            light_info=merge_light_infos([_light_info(light) for light in selected]),
            effects=common_effects([light.effect_list or [] for light in selected]),
        )

    def _estimate_dummy_light(self, request: LightMeasurementRequest) -> PreflightResult:
        controller = DummyLightController()
        plan = build_light_plan(
            request.modes,
            request.parameters,
            controller.get_light_info(),
            controller.get_effect_list(),
        )
        remaining = self._remaining_light_variations(request, plan.variations)
        return PreflightResult(
            estimated_variations=len(remaining),
            estimated_duration_seconds=round(
                estimate_light_time_left(plan, request.parameters, remaining_variations=remaining),
            ),
            supported_modes=tuple(sorted(request.modes, key=str)),
        )

    def _remaining_light_variations(
        self,
        request: LightMeasurementRequest,
        plan_variations: Sequence[Variation],
    ) -> list[Variation]:
        if (
            request.resume_policy != ResumePolicy.EXTEND
            or not request.seed_session_id
            or request.remeasure_existing
            or self._load_measured_variations is None
        ):
            return list(plan_variations)
        measured = {
            variation for keys in self._load_measured_variations(request.seed_session_id).values() for variation in keys
        }
        return missing_variations(plan_variations, measured)

    def _require_entity(self, entity_id: str | None, domain: EntityDomain, message: str) -> EntityRecord:
        available = {entity.entity_id: entity for entity in self._load_entities(domain, None)}
        entity = available.get(entity_id or "")
        if entity is None:
            raise PreflightError(message)
        return entity


def estimate_light_measurement(
    request: LightMeasurementRequest,
    *,
    load_entities: EntityLoader | None = None,
    load_measured_variations: Callable[[str], Mapping[LutMode, Collection[Variation]]] | None = None,
    load_measured_points: Callable[[str], Mapping[LutMode, Sequence[MeasuredPoint]]] | None = None,
    load_historical_runs: Callable[[], Sequence[HistoricalRunTiming]] | None = None,
) -> LightPlanEstimate:
    """Build the cheap setup-page estimate: plan axes only, no probe or session lock."""

    light_info, effects, used_default_range = _estimate_light_context(request, load_entities)
    summaries = summarize_light_modes(request.modes, request.parameters, light_info, effects)
    modes = tuple(
        LightModeEstimate(mode=mode, axes=dict(axes), points=points, summary=summary)
        for mode, axes, points, summary in summaries
    )
    total_points = sum(item.points for item in modes)
    remaining_points: int | None = None
    if (
        request.resume_policy == ResumePolicy.EXTEND
        and request.seed_session_id
        and not request.remeasure_existing
        and (load_measured_variations is not None or load_measured_points is not None)
    ):
        if load_measured_points is not None:
            points_by_mode = load_measured_points(request.seed_session_id)
            measured_by_mode = {mode: {point.variation for point in points} for mode, points in points_by_mode.items()}
        else:
            assert load_measured_variations is not None
            points_by_mode = {}
            measured_by_mode = load_measured_variations(request.seed_session_id)
        measured = {variation for keys in measured_by_mode.values() for variation in keys}
        plan = build_light_plan(request.modes, request.parameters, light_info, effects)
        if any(smart_applies(item.mode, request.parameters) for item in modes):
            remaining_by_mode: dict[LutMode, int] = {}
            for item in modes:
                if smart_applies(item.mode, request.parameters):
                    remaining_by_mode[item.mode] = estimate_smart_remaining(
                        item.mode,
                        request.parameters,
                        light_info,
                        measured_by_mode.get(item.mode, ()),
                        measured_points=points_by_mode.get(item.mode),
                    )
                else:
                    planned = [variation for variation in plan.variations if variation.mode == item.mode]
                    remaining_by_mode[item.mode] = len(missing_variations(planned, measured))
            remaining_points = sum(remaining_by_mode.values())
            modes = tuple(
                LightModeEstimate(
                    mode=item.mode,
                    axes=item.axes,
                    points=item.points,
                    summary=(
                        "seed already covers this Δ — nothing more to measure"
                        if remaining_by_mode.get(item.mode, 0) == 0 and smart_applies(item.mode, request.parameters)
                        else item.summary
                    ),
                )
                for item in modes
            )
            max_duration = round(
                estimate_light_run_seconds(
                    {mode: count for mode, count in remaining_by_mode.items() if count > 0},
                    request.parameters,
                    effects,
                )
            )
            counted = remaining_points
        else:
            remaining = missing_variations(plan.variations, measured)
            remaining_points = len(remaining)
            max_duration = round(estimate_light_time_left(plan, request.parameters, remaining_variations=remaining))
            counted = remaining_points
    else:
        duration_points = {item.mode: item.points + int(item.axes.get("coverage_cap") or 0) for item in modes}
        max_duration = round(estimate_light_run_seconds(duration_points, request.parameters, effects))
        counted = sum(duration_points.values()) if request.parameters.smart_sampling else total_points
    sample_count = request.parameters.sample_count
    estimated_duration_seconds: int | None = None
    estimated_from_runs: int | None = None
    if load_historical_runs is not None:
        rate = historical_seconds_per_point(request, load_historical_runs())
        if rate is not None:
            seconds_per_point, estimated_from_runs = rate
            estimated_duration_seconds = max(0, round(seconds_per_point * counted))
    return LightPlanEstimate(
        modes=modes,
        total_points=counted if remaining_points is not None or request.parameters.smart_sampling else total_points,
        total_readings=counted * sample_count if sample_count > 1 else None,
        max_duration_seconds=max_duration,
        used_default_range=used_default_range,
        remaining_points=remaining_points,
        estimated_duration_seconds=estimated_duration_seconds,
        estimated_from_runs=estimated_from_runs,
    )


def _estimate_light_context(
    request: LightMeasurementRequest,
    load_entities: EntityLoader | None,
) -> tuple[LightInfo, list[str], bool]:
    dummy = DummyLightController()
    if isinstance(request.controller, DummyLightControllerSpec) or load_entities is None:
        return dummy.get_light_info(), dummy.get_effect_list() if LutMode.EFFECT in request.modes else [], True
    if not isinstance(request.controller, HassLightControllerSpec | HassMultiLightControllerSpec):
        return dummy.get_light_info(), dummy.get_effect_list() if LutMode.EFFECT in request.modes else [], True
    lights = {entity.entity_id: entity for entity in load_entities(EntityDomain.LIGHT, None)}
    selected = [lights[entity_id] for entity_id in request.controller.entity_ids if entity_id in lights]
    if len(selected) != len(request.controller.entity_ids) or not selected:
        return dummy.get_light_info(), dummy.get_effect_list() if LutMode.EFFECT in request.modes else [], True
    return (
        merge_light_infos([_light_info(light) for light in selected]),
        common_effects([light.effect_list or [] for light in selected]),
        False,
    )
