from collections.abc import Collection, Iterable, Mapping
from enum import StrEnum
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from measure.const import (
    CT_MIRED_DIVISIONS_ABSOLUTE_MAX,
    MANUAL_PARAMETER_LIMIT_OVERRIDES,
    PARAMETER_LIMITS,
    MeasureType,
)
from measure.controller.charging.const import ChargingDeviceType
from measure.controller.charging.spec import ChargingControllerSpec
from measure.controller.fan.spec import FanControllerSpec
from measure.controller.light.capabilities import kelvin_range_to_mired
from measure.controller.light.const import LutMode
from measure.controller.light.spec import LightControllerSpec
from measure.controller.media.spec import MediaControllerSpec
from measure.controller.spec import BaseControllerSpec
from measure.powermeter.spec import (
    CompositePowerMeterSpec,
    DummyPowerMeterSpec,
    ManualPowerMeterSpec,
    OcrPowerMeterSpec,
    PowerMeterSpec,
)
from measure.runner.const import COMPLEX_PROFILE_EXPORT_FILENAME, DEFAULT_EXPORT_FILENAME
from measure.tuning import MeasurementParameters


class ResumePolicy(StrEnum):
    NEW = "new"
    RESUME = "resume"
    EXTEND = "extend"


class RecorderPurpose(StrEnum):
    PLAYBOOK = "playbook"
    COMPLEX_PROFILE = "complex_profile"


class RecorderProfileRecipe(StrEnum):
    GENERIC = "generic"
    VACUUM_ROBOT = "vacuum_robot"


class DummyLoadCalibrationRequest(BaseModel):
    """Request calibration of a physical resistive dummy load before measuring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["calibrate"] = "calibrate"
    description: str = Field(min_length=1, max_length=200)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("dummy-load description is required")
        return value


class DummyLoadReuseRequest(BaseModel):
    """Use a previously calibrated physical resistive dummy load."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["reuse"] = "reuse"
    description: str = Field(min_length=1, max_length=200)
    resistance: float = Field(gt=0)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("dummy-load description is required")
        return value


type DummyLoadRequest = Annotated[
    DummyLoadCalibrationRequest | DummyLoadReuseRequest,
    Field(discriminator="mode"),
]


_BASE_PARAMETER_FIELDS = ("sleep_time", "sample_count", "sleep_time_sample", "max_retries", "max_nudges")
_LIGHT_PARAMETER_FIELDS = (
    "min_brightness",
    "max_brightness",
    "min_kelvin",
    "max_kelvin",
    "min_sat",
    "max_sat",
    "min_hue",
    "max_hue",
    "settle_tolerance_pct",
    "settle_tolerance_w",
    "settle_window_seconds",
    "settle_poll_interval_seconds",
    "settle_min_wait",
    "bri_bri_steps",
    "ct_bri_steps",
    "ct_mired_divisions",
    "hs_bri_steps",
    "hs_hue_divisions",
    "hs_sat_divisions",
    "effect_bri_steps",
    "sleep_initial",
    "sleep_standby",
    "measure_time_effect",
    "measure_time_effect_min",
    "smart_delta",
    "smart_border_delta",
    "smart_dart_min_delta",
)

#: Old native-increment keys persisted before those axes became sweep counts.
_LEGACY_INCREMENT_PARAMETER_KEYS = frozenset(
    {"ct_mired_steps", "hs_hue_steps", "hs_sat_steps"},
)

#: Sweep-count / step fields whose All checkbox ignores the submitted number.
_ALL_IGNORES_FIELD = {
    "bri_bri_steps": "bri_bri_all",
    "ct_bri_steps": "ct_bri_all",
    "hs_bri_steps": "hs_bri_all",
    "effect_bri_steps": "effect_bri_all",
    "ct_mired_divisions": "ct_mired_all",
    "hs_hue_divisions": "hs_hue_all",
    "hs_sat_divisions": "hs_sat_all",
}

#: Parameters a LUT mode owns. Hidden leftovers (defaults, or a previous run) must not
#: be judged against the live axis span of a mode that is not selected — raising
#: min_brightness to 223 used to reject the unused `effect_bri_steps: 40` default.
_MODE_GATED_PARAMETERS: dict[LutMode, frozenset[str]] = {
    LutMode.BRIGHTNESS: frozenset({"bri_bri_steps"}),
    LutMode.COLOR_TEMP: frozenset(
        {"ct_bri_steps", "ct_mired_divisions", "smart_delta", "smart_border_delta", "smart_dart_min_delta"},
    ),
    LutMode.HS: frozenset(
        {
            "hs_bri_steps",
            "hs_hue_divisions",
            "hs_sat_divisions",
            "smart_delta",
            "smart_border_delta",
            "smart_dart_min_delta",
        },
    ),
    LutMode.EFFECT: frozenset({"effect_bri_steps", "measure_time_effect", "measure_time_effect_min"}),
}
_COLOR_MODES = frozenset({LutMode.COLOR_TEMP, LutMode.HS})
_GATED_PARAMETERS = frozenset.union(*_MODE_GATED_PARAMETERS.values())


# Headroom above rated power before an OCR reading is rejected as implausible: generous
# enough for power-factor correction inrush and dimming-curve nonlinearity, tight enough
# to still catch the failure mode this exists for -- a misread that inflates every field
# by the same wrong factor (a missed decimal point, say), which the V*I*PF crosscheck
# alone can't catch since it stays internally consistent.
_OCR_RATED_POWER_MARGIN = 3.0


def _with_ocr_plausibility_bound(spec: PowerMeterSpec, bound: float) -> PowerMeterSpec:
    """Fill `max_plausible_power_w` on every OCR meter in `spec` that doesn't already have
    its own explicit bound, recursing into a composite's primary and witnesses. Returns
    `spec` unchanged (same object) if nothing needed filling in.
    """
    if isinstance(spec, OcrPowerMeterSpec):
        if spec.max_plausible_power_w is not None:
            return spec
        return spec.model_copy(update={"max_plausible_power_w": bound})
    if isinstance(spec, CompositePowerMeterSpec):
        primary = _with_ocr_plausibility_bound(spec.primary, bound)
        witnesses = []
        witnesses_changed = False
        for witness in spec.witnesses:
            meter = _with_ocr_plausibility_bound(witness.meter, bound)
            if meter is witness.meter:
                witnesses.append(witness)
            else:
                witnesses.append(witness.model_copy(update={"meter": meter}))
                witnesses_changed = True
        if primary is spec.primary and not witnesses_changed:
            return spec
        return spec.model_copy(update={"primary": primary, "witnesses": witnesses})
    return spec


def _axis_span(low: int, high: int) -> int:
    return max(1, high - low + 1)


def _live_parameter_limits(parameters: MeasurementParameters) -> dict[str, tuple[float, float]]:
    """Slider-matching caps: sweep counts and steps cannot exceed the live discrete span."""

    brightness = _axis_span(parameters.min_brightness, parameters.max_brightness)
    saturation = _axis_span(parameters.min_sat, parameters.max_sat)
    hue = _axis_span(parameters.min_hue, parameters.max_hue)
    cool_mired, warm_mired = kelvin_range_to_mired(parameters.min_kelvin, parameters.max_kelvin)
    kelvin_lo, kelvin_hi = PARAMETER_LIMITS["min_kelvin"]
    return {
        "min_brightness": (1, parameters.max_brightness),
        "max_brightness": (parameters.min_brightness, 255),
        "min_kelvin": (kelvin_lo, parameters.max_kelvin),
        "max_kelvin": (parameters.min_kelvin, kelvin_hi),
        "min_sat": (1, parameters.max_sat),
        "max_sat": (parameters.min_sat, 255),
        "min_hue": (1, parameters.max_hue),
        "max_hue": (parameters.min_hue, 65535),
        "bri_bri_steps": (1, brightness),
        "ct_bri_steps": (1, brightness),
        "hs_bri_steps": (1, brightness),
        "effect_bri_steps": (1, brightness),
        "hs_sat_divisions": (1, saturation),
        "hs_hue_divisions": (3, hue),
        "ct_mired_divisions": (1, min(CT_MIRED_DIVISIONS_ABSOLUTE_MAX, _axis_span(cool_mired, warm_mired))),
    }


def _active_light_parameter_fields(modes: Collection[LutMode]) -> tuple[str, ...]:
    """Tuning fields that the selected LUT modes actually consume."""

    active_gated = set().union(*(_MODE_GATED_PARAMETERS[mode] for mode in modes if mode in _MODE_GATED_PARAMETERS))
    if modes & _COLOR_MODES:
        active_gated.discard("bri_bri_steps")
    return tuple(name for name in _LIGHT_PARAMETER_FIELDS if name not in _GATED_PARAMETERS or name in active_gated)


def _step_unused_on_collapsed_axis(
    name: str,
    overrides: Mapping[str, tuple[float, float]],
) -> bool:
    """True when this sweep/step field cannot choose more than one sample.

    Pinning min brightness to max (a 100%-only HS ring, for example) makes the
    leftover default ``hs_bri_steps: 32`` meaningless — the same reason All
    skips the number. Judging it against a (1, 1) cap is what rejected that.
    """

    bounds = overrides.get(name)
    if bounds is None:
        return False
    minimum, maximum = bounds
    return maximum <= 1 or maximum < minimum


def _validate_parameter_limits(
    parameters: MeasurementParameters,
    names: Iterable[str],
    overrides: dict[str, tuple[float, float]] | None = None,
) -> None:
    for name in names:
        minimum, maximum = (overrides or {}).get(name, PARAMETER_LIMITS[name])
        number = getattr(parameters, name)
        if not minimum <= number <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")


class BaseMeasurementRequest(BaseModel):
    """Complete validated description of one measurement run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    measure_type: MeasureType
    model_id: str = Field(default="", max_length=120)
    product_name: str = Field(default="", max_length=200)
    session_name: str = Field(default="", max_length=200)
    measure_device: str = Field(default="", max_length=200)
    power_meter: PowerMeterSpec
    parameters: MeasurementParameters = Field(default_factory=MeasurementParameters)
    generate_model: bool = False
    fast_test_mode: bool = False
    resume_policy: ResumePolicy = ResumePolicy.NEW
    seed_session_id: str | None = None
    remeasure_existing: bool = False
    derived_from: tuple[str, ...] = ()
    dummy_load: DummyLoadRequest | None = None
    # Measure types that drive a device (light/speaker/charging/fan) narrow this to their
    # own required, discriminated controller spec; average/recorder leave it as None.
    controller: BaseControllerSpec | None = None

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        if value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()+-]*", value):
            raise ValueError("model_id contains unsafe characters")
        return value

    @field_validator("product_name", "measure_device", "session_name", mode="before")
    @classmethod
    def normalize_profile_metadata(cls, value: str) -> str:
        return value.strip()

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: MeasurementParameters) -> MeasurementParameters:
        _validate_parameter_limits(value, _BASE_PARAMETER_FIELDS)
        return value

    @model_validator(mode="after")
    def validate_dummy_load_power_meter(self) -> BaseMeasurementRequest:
        if self.dummy_load is not None and isinstance(self.power_meter, DummyPowerMeterSpec):
            raise ValueError("A resistive dummy load cannot be used with the synthetic test power meter")
        return self

    @model_validator(mode="after")
    def validate_extend_seed(self) -> BaseMeasurementRequest:
        if self.resume_policy == ResumePolicy.EXTEND:
            if not self.seed_session_id:
                raise ValueError("resume_policy=extend requires seed_session_id")
        elif self.seed_session_id:
            raise ValueError("seed_session_id is only valid with resume_policy=extend")
        return self

    @property
    def controlled_entity_ids(self) -> tuple[str, ...]:
        """Home Assistant entities driven during the measurement, empty when the controller drives none."""
        entity_ids = getattr(self.controller, "entity_ids", None) or [getattr(self.controller, "entity_id", None)]
        return tuple(str(entity_id) for entity_id in entity_ids if entity_id)

    @property
    def model_name(self) -> str:
        return self.product_name

    @property
    def generate_model_json(self) -> bool:
        return self.generate_model


class LightMeasurementRequest(BaseMeasurementRequest):
    measure_type: Literal[MeasureType.LIGHT] = MeasureType.LIGHT
    measure_device: str = Field(min_length=1, max_length=200)
    controller: LightControllerSpec
    modes: set[LutMode] = Field(default_factory=lambda: {LutMode.BRIGHTNESS}, min_length=1)
    generate_model: bool = True
    gzip: bool = True
    multiple_light_count: int = Field(default=1, ge=1, le=100)
    # Optional; only used to derive a plausibility bound for an OCR power meter (primary
    # or witness) so a misread that inflates every field by the same wrong factor -- which
    # the V*I*PF crosscheck alone can't catch -- still gets rejected. Left unset, no such
    # bound is applied. Per-unit, before `multiple_light_count` is factored in.
    rated_power_w: float | None = Field(default=None, gt=0, le=100_000)

    @field_validator("modes")
    @classmethod
    def validate_modes(cls, value: set[LutMode]) -> set[LutMode]:
        unsupported = value - {LutMode.BRIGHTNESS, LutMode.COLOR_TEMP, LutMode.HS, LutMode.EFFECT}
        if unsupported:
            raise ValueError(f"Unsupported measurement modes: {', '.join(sorted(unsupported))}")
        return value

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_native_increment_fields(cls, data: object) -> object:
        """``ct_mired_steps`` / ``hs_hue_steps`` / ``hs_sat_steps`` were native increments.

        Those axes are now sweep counts (``*_divisions``). The leftover keys cannot be
        translated — 10-as-mired-step is not 10 sweeps, and 2731-as-hue-step is the old
        default increment, not 2731 divisions — so they are dropped and the divisions
        defaults apply.
        """
        if not isinstance(data, dict):
            return data
        parameters = data.get("parameters")
        if not isinstance(parameters, dict):
            return data
        leftover = _LEGACY_INCREMENT_PARAMETER_KEYS.intersection(parameters)
        if not leftover:
            return data
        cleaned = {key: value for key, value in parameters.items() if key not in leftover}
        return {**data, "parameters": cleaned}

    @model_validator(mode="after")
    def validate_light_parameters(self) -> LightMeasurementRequest:
        # Live axis spans replace the static sat-32 / hue-360 / mired-129 caps so a slider
        # value the UI allows is not rejected here. Manual meters still cap how fine CT
        # can go because hand-reading every point is laborious.
        value = self.parameters
        overrides = _live_parameter_limits(value)
        if isinstance(self.power_meter, ManualPowerMeterSpec):
            overrides.update(MANUAL_PARAMETER_LIMIT_OVERRIDES)
        if value.min_brightness > value.max_brightness:
            raise ValueError("min_brightness must not exceed max_brightness")
        if value.min_kelvin > value.max_kelvin:
            raise ValueError("min_kelvin must not exceed max_kelvin")
        if value.min_sat > value.max_sat:
            raise ValueError("min_sat must not exceed max_sat")
        if value.min_hue > value.max_hue:
            raise ValueError("min_hue must not exceed max_hue")
        checked = [
            name
            for name in _active_light_parameter_fields(self.modes)
            if not getattr(value, _ALL_IGNORES_FIELD.get(name, ""), False)
            if not _step_unused_on_collapsed_axis(name, overrides)
        ]
        _validate_parameter_limits(value, checked, overrides)
        if value.measure_time_effect_min > value.measure_time_effect:
            raise ValueError("measure_time_effect_min must not exceed measure_time_effect")
        if not value.hs_hue_all and _axis_span(value.min_hue, value.max_hue) >= 3 and value.hs_hue_divisions % 3 != 0:
            # The hue wheel is seeded at the three RGB(WW) emitter hues and trisected from
            # there (see `_circular_trisection_order`), so any count that isn't a multiple
            # of 3 can't be evenly split into that scheme -- there's no principled way to
            # pick which of the three arcs gets the extra point. Hue All walks every
            # integer and skips this rule.
            raise ValueError("hs_hue_divisions must be a multiple of 3")
        if value.settle_tolerance_pct > 0 and isinstance(self.power_meter, ManualPowerMeterSpec):
            # Polling a manual meter means prompting a human for a value on every poll.
            raise ValueError("settle_tolerance_pct requires a polled power meter, not manual entry")
        return self

    @model_validator(mode="after")
    def apply_rated_power_to_ocr_meters(self) -> LightMeasurementRequest:
        """Derive every OCR meter's plausibility bound from `rated_power_w`, if given.

        Applies to the primary and any witnesses alike (a witness OCR meter reads the same
        physical device, or one wired in series with it, so the same rating applies), and
        only fills meters that don't already carry their own explicit bound.
        """
        if self.rated_power_w is None:
            return self
        bound = self.rated_power_w * self.multiple_light_count * _OCR_RATED_POWER_MARGIN
        power_meter = _with_ocr_plausibility_bound(self.power_meter, bound)
        if power_meter is self.power_meter:
            return self
        return self.model_copy(update={"power_meter": power_meter})


class AverageMeasurementRequest(BaseMeasurementRequest):
    measure_type: Literal[MeasureType.AVERAGE] = MeasureType.AVERAGE
    controller: None = None
    duration: int = Field(default=60, ge=1, le=86_400)


class RecorderMeasurementRequest(BaseMeasurementRequest):
    measure_type: Literal[MeasureType.RECORDER] = MeasureType.RECORDER
    controller: None = None
    recorder_purpose: RecorderPurpose = RecorderPurpose.PLAYBOOK
    profile_recipe: RecorderProfileRecipe | None = None
    tracked_entity_ids: tuple[str, ...] = Field(default=(), max_length=100)
    vacuum_entity_id: str | None = None
    battery_entity_id: str | None = None
    additional_entity_ids: tuple[str, ...] = Field(default=(), max_length=100)
    export_filename: str = Field(default=DEFAULT_EXPORT_FILENAME, min_length=1, max_length=200)

    @property
    def generate_model_json(self) -> bool:
        """Recorder model generation is handled by the analyser after capture."""

        return False

    @model_validator(mode="before")
    @classmethod
    def select_export_filename(cls, data: object) -> object:
        """Use the fixed filename for the selected recorder output format."""

        if not isinstance(data, dict):
            return data
        filename = (
            COMPLEX_PROFILE_EXPORT_FILENAME
            if data.get("recorder_purpose") == RecorderPurpose.COMPLEX_PROFILE
            else DEFAULT_EXPORT_FILENAME
        )
        return data | {"export_filename": filename}

    @field_validator(
        "tracked_entity_ids",
        "additional_entity_ids",
        mode="after",
    )
    @classmethod
    def validate_entity_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_entity_id(value)
        return values

    @field_validator("vacuum_entity_id", "battery_entity_id", mode="after")
    @classmethod
    def validate_optional_entity_id(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_entity_id(value)
        return value

    @model_validator(mode="after")
    def validate_recorder_selection(self) -> RecorderMeasurementRequest:
        if self.recorder_purpose == RecorderPurpose.PLAYBOOK:
            if self._has_profile_selection():
                raise ValueError("Playbook recordings cannot include complex-profile entity selections")
            return self

        if self.profile_recipe is None:
            raise ValueError("profile_recipe is required for a complex-profile recording")
        if self.profile_recipe == RecorderProfileRecipe.GENERIC:
            self._validate_generic_selection()
        else:
            self._validate_vacuum_selection()

        entity_ids = self.recorded_entity_ids
        if len(entity_ids) > 100:
            raise ValueError("A recorder session can track at most 100 entities")
        if len(set(entity_ids)) != len(entity_ids):
            raise ValueError("Recorder entity selections must be unique")
        return self

    def _has_profile_selection(self) -> bool:
        return bool(
            self.profile_recipe
            or self.tracked_entity_ids
            or self.vacuum_entity_id
            or self.battery_entity_id
            or self.additional_entity_ids
        )

    def _validate_generic_selection(self) -> None:
        if not self.tracked_entity_ids:
            raise ValueError("Select at least one entity for a generic complex-profile recording")
        if self.vacuum_entity_id or self.battery_entity_id or self.additional_entity_ids:
            raise ValueError("Generic recordings cannot include vacuum-recipe entity selections")

    def _validate_vacuum_selection(self) -> None:
        if self.tracked_entity_ids:
            raise ValueError("Vacuum recordings cannot include generic tracked entities")
        if self.vacuum_entity_id is None or self.battery_entity_id is None:
            raise ValueError("A vacuum and battery entity are required for a vacuum recording")
        if not self.vacuum_entity_id.startswith("vacuum."):
            raise ValueError("vacuum_entity_id must be a vacuum entity")
        if not self.battery_entity_id.startswith("sensor."):
            raise ValueError("battery_entity_id must be a sensor entity")

    @property
    def recorded_entity_ids(self) -> tuple[str, ...]:
        """Entities recorded in deterministic capture order."""

        if self.recorder_purpose == RecorderPurpose.PLAYBOOK:
            return ()
        if self.profile_recipe == RecorderProfileRecipe.GENERIC:
            return self.tracked_entity_ids
        return tuple(
            entity_id
            for entity_id in (self.vacuum_entity_id, self.battery_entity_id, *self.additional_entity_ids)
            if entity_id is not None
        )


class SpeakerMeasurementRequest(BaseMeasurementRequest):
    measure_type: Literal[MeasureType.SPEAKER] = MeasureType.SPEAKER
    controller: MediaControllerSpec
    disable_streaming: bool = False
    generate_model: bool = True


class ChargingMeasurementRequest(BaseMeasurementRequest):
    measure_type: Literal[MeasureType.CHARGING] = MeasureType.CHARGING
    controller: ChargingControllerSpec
    charging_device_type: ChargingDeviceType
    generate_model: bool = True


class FanMeasurementRequest(BaseMeasurementRequest):
    measure_type: Literal[MeasureType.FAN] = MeasureType.FAN
    controller: FanControllerSpec
    generate_model: bool = True


type MeasurementRequest = (
    LightMeasurementRequest
    | AverageMeasurementRequest
    | RecorderMeasurementRequest
    | SpeakerMeasurementRequest
    | ChargingMeasurementRequest
    | FanMeasurementRequest
)

MeasurementRequestPayload = Annotated[MeasurementRequest, Field(discriminator="measure_type")]
_REQUEST_ADAPTER: TypeAdapter[MeasurementRequest] = TypeAdapter(MeasurementRequestPayload)


def parse_measurement_request(data: object) -> MeasurementRequest:
    """Validate persisted input using the measurement and adapter discriminators."""
    return _REQUEST_ADAPTER.validate_python(data)


def validate_export_filename(value: str) -> str:
    """Return a safe recorder basename which cannot escape its output directory."""
    value = value.strip()
    if value in {"", ".", ".."} or value != value.replace("\\", "/").rsplit("/", 1)[-1]:
        raise ValueError("export_filename must be a file name without directory components")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()+-]*", value):
        raise ValueError("export_filename contains unsafe characters")
    return value


def _validate_entity_id(value: str) -> None:
    if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", value):
        raise ValueError(f"Invalid Home Assistant entity ID: {value}")
