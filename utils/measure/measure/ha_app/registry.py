from dataclasses import dataclass
from enum import StrEnum

from measure.const import MEASURE_TYPE_LABELS, MeasureType
from measure.controller.charging.const import ChargingDeviceType
from measure.controller.charging.spec import charging_entity_domain
from measure.controller.light.const import LutMode
from measure.request import RecorderProfileRecipe, RecorderPurpose


class FieldControl(StrEnum):
    ENTITY = "entity"
    NUMBER = "number"
    TEXT = "text"
    BOOLEAN = "boolean"
    SELECT = "select"
    MULTI_SELECT = "multi_select"


class FieldRole(StrEnum):
    """Where a submitted field value lands in the measurement request.

    Clients build the request from this alone: a CONTROLLER field becomes the
    discriminated ``controller`` spec, POWER_METER is supplied by the client's own
    power-meter configuration, and an ATTRIBUTE sets the request attribute that
    carries the same name as the field.
    """

    ATTRIBUTE = "attribute"
    CONTROLLER = "controller"
    POWER_METER = "power_meter"


@dataclass(frozen=True)
class FieldOption:
    value: str
    label: str
    entity_domain: str | None = None
    #: Measurement parameters that only apply while this option is selected.
    enables: tuple[str, ...] = ()
    description: str = ""
    guidance: tuple[str, ...] = ()


@dataclass(frozen=True)
class FormFieldDefinition:
    name: str
    label: str
    control: FieldControl
    role: FieldRole = FieldRole.ATTRIBUTE
    #: Field whose current value constrains this one. A multi-select names the controller field
    #: whose entity limits its options; an entity field names the select whose chosen option
    #: supplies the entity domain to offer.
    narrowed_by: str | None = None
    required: bool = True
    entity_domains: tuple[str, ...] = ()
    options: tuple[FieldOption, ...] = ()
    default: str | int | bool | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    #: HTML `step` for a NUMBER control. `None` renders no `step` attribute, which every
    #: browser treats as the number-input default of `1` -- fine for an inherently integer
    #: field (a count of lights, a duration in seconds), wrong for anything that can
    #: genuinely take a decimal (confirmed 2026-09-06: a fractional watt value was
    #: rejected outright). Set explicitly per field rather than defaulting to "any" here,
    #: so a field that really is integer-only keeps native step validation instead of
    #: silently accepting fractions.
    step: str | None = None
    #: Whether several entities can be selected for this field at once.
    multiple: bool = False
    #: Label to use while several entities are selected.
    plural_label: str = ""
    #: Entity field whose number of selected entities this count follows by default.
    derived_from: str | None = None
    hint: str = ""
    #: Other field values required for this field to be shown.
    visible_when: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Load the full Home Assistant entity catalog rather than one supported controller domain.
    all_entities: bool = False
    entity_device_classes: tuple[str, ...] = ()
    #: Entity field whose Home Assistant device should be preferred or required.
    related_to: str | None = None
    same_device_only: bool = False
    #: Restate this field on the review screen.
    review: bool = False


class ParameterControl(StrEnum):
    NUMBER = "number"
    BOOLEAN = "boolean"


class ParameterAxis(StrEnum):
    BRIGHTNESS = "brightness"
    SAT = "sat"
    HUE = "hue"
    MIRED = "mired"
    KELVIN = "kelvin"


@dataclass(frozen=True)
class ParameterDefinition:
    """A measurement tuning parameter as one measure type presents it.

    The same parameter can read differently per type — a light settles, a recorder
    samples — so the wording lives here rather than in the client. Bounds come from
    ``PARAMETER_LIMITS`` via the capabilities endpoint, then the selected light's
    live span when ``axis`` is set.
    """

    name: str
    label: str
    hint: str = ""
    step: str = "1"
    #: Heading this parameter is grouped under, repeated on each member of the group.
    group: str = ""
    #: Only applies while the named parameter is greater than one.
    requires_multiple: str | None = None
    control: ParameterControl = ParameterControl.NUMBER
    #: Bisect checkbox bound to this parameter name, shown beside the number.
    bisection: str | None = None
    #: All checkbox bound to this parameter name, shown beside the number.
    all_values: str | None = None
    #: Label used while Bisect or All is on, for brightness fields that switch meaning.
    sweep_label: str | None = None
    #: Discrete axis whose live span is this slider's max once a light is selected.
    axis: ParameterAxis | None = None


@dataclass(frozen=True)
class MeasurementDefinition:
    measure_type: MeasureType
    description: str
    icon: str
    confirmation_action: str | None = None
    #: Present the confirmation as a warning, for a measurement that makes noise or mess.
    confirmation_is_warning: bool = False
    #: Placeholders steering the profile fields, taken from real entries in the profile library.
    model_id_example: str = ""
    product_name_example: str = ""
    fields: tuple[FormFieldDefinition, ...] = ()
    #: Tuning parameters this measure type exposes, in the order they are shown.
    parameters: tuple[ParameterDefinition, ...] = ()
    supports_profile: bool = True
    supports_resume: bool = False

    @property
    def label(self) -> str:
        return MEASURE_TYPE_LABELS[self.measure_type]


def _controller(
    name: str,
    label: str,
    *domains: str,
    narrowed_by: str | None = None,
    multiple: bool = False,
    plural_label: str = "",
) -> FormFieldDefinition:
    """Entity field that selects the device being measured, and becomes the request controller."""
    return FormFieldDefinition(
        name=name,
        label=label,
        control=FieldControl.ENTITY,
        role=FieldRole.CONTROLLER,
        narrowed_by=narrowed_by,
        entity_domains=domains,
        multiple=multiple,
        plural_label=plural_label,
    )


#: The lookup-table modes a light can be profiled in, each with the resolution parameters it
#: activates. A light only offers the subset its entity reports as supported.
LIGHT_MODE_OPTIONS = (
    FieldOption(value=LutMode.BRIGHTNESS, label="Brightness", enables=("bri_bri_steps",)),
    FieldOption(
        value=LutMode.COLOR_TEMP,
        label="Color temperature",
        enables=(
            "ct_bri_steps",
            "ct_mired_divisions",
            "min_kelvin",
            "max_kelvin",
            "smart_sampling",
            "smart_delta",
            "smart_border_delta",
            "smart_dart",
            "smart_dart_min_delta",
        ),
    ),
    FieldOption(
        value=LutMode.HS,
        label="Hue & saturation",
        enables=(
            "hs_bri_steps",
            "hs_hue_divisions",
            "hs_sat_divisions",
            "min_sat",
            "max_sat",
            "min_hue",
            "max_hue",
            "smart_sampling",
            "smart_delta",
            "smart_border_delta",
            "smart_dart",
            "smart_dart_min_delta",
        ),
    ),
    FieldOption(
        value=LutMode.EFFECT,
        label="Effect",
        enables=("effect_bri_steps", "measure_time_effect_min", "measure_time_effect"),
    ),
)

MODES_FIELD = FormFieldDefinition(
    name="modes",
    label="Lookup-table modes",
    control=FieldControl.MULTI_SELECT,
    narrowed_by="light_entity_id",
    options=LIGHT_MODE_OPTIONS,
)

SAMPLING = "Sampling"
RANGE_BOUNDS = "Range bounds"
RESOLUTION = "Profile resolution"


def _sampling(label: str, hint: str) -> tuple[ParameterDefinition, ...]:
    """Repeated-reading parameters, worded for the type that takes the readings."""
    return (
        ParameterDefinition(name="sample_count", label=label, hint=hint, group=SAMPLING),
        ParameterDefinition(
            name="sleep_time_sample",
            label="Time between samples (seconds)",
            hint="Only used when taking more than one sample.",
            step="0.1",
            group=SAMPLING,
            requires_multiple="sample_count",
        ),
    )


READING_INTERVAL = ParameterDefinition(
    name="sleep_time",
    label="Reading interval (seconds)",
    hint="Delay between repeated power readings and retries.",
    step="0.1",
    group=SAMPLING,
)

POINT_SAMPLING = _sampling("Samples per reading", "More samples reduce noise but increase measurement time.")

LIGHT_PARAMETERS = (
    ParameterDefinition(
        name="sleep_time",
        label="Settle time (seconds)",
        hint=(
            "Wait after changing the light before reading power. Acts as an upper bound, "
            "not a fixed wait, when settle detection below is enabled."
        ),
        step="0.1",
        group=SAMPLING,
    ),
    ParameterDefinition(
        name="settle_tolerance_pct",
        label="Settle detection tolerance (%)",
        hint=(
            "0 disables this and always waits the full settle time above. Above 0, proceed "
            "once the reading has held within this tolerance for a second (at least three "
            "polls in that window), instead of always waiting the full settle time -- "
            "falls back to the full wait if it never stabilizes. Combined with the watt "
            "floor below: a track is flat when its spread is within this percentage or "
            "that many watts, whichever is larger. The minimum wait below still runs "
            "first so a leftover flat reading cannot be accepted immediately. Requires a "
            "polled power meter (not manual entry)."
        ),
        step="0.01",
        group=SAMPLING,
    ),
    ParameterDefinition(
        name="settle_tolerance_w",
        label="Settle detection floor (W)",
        hint=(
            "Also treat a track as settled when its spread is within this many watts, even "
            "if that is more than the percentage above. Covers meters that only report "
            "0.1 W steps (Shelly) at low load, where one step is already several percent. "
            "0 uses the percentage only."
        ),
        step="0.01",
        group=SAMPLING,
    ),
    ParameterDefinition(
        name="settle_min_wait",
        label="Minimum wait before settle (seconds)",
        hint=(
            "Blind wait after the light command (and after Home Assistant reports the "
            "new brightness) before settle detection may accept a plateau. Hardware "
            "ramps and HA lag often leave the previous power reading flat for a moment "
            "— without this, settle can record the last point again, including while "
            "the lamp is still off. Unused when settle detection is off. 0 disables."
        ),
        step="0.1",
        group=SAMPLING,
    ),
    *_sampling("Samples per point", "More samples reduce noise but increase measurement time."),
    ParameterDefinition(
        name="sleep_initial",
        label="Initial stabilization (seconds)",
        hint=(
            "Extra wait after the first point of a mode, for fixed-sleep runs. Unused "
            "when settle detection is on — the plateau wait already covers leftover "
            "max-load readings."
        ),
        step="0.1",
        group=SAMPLING,
    ),
    ParameterDefinition(
        name="sleep_standby",
        label="Standby stabilization (seconds)",
        step="0.1",
        group=SAMPLING,
    ),
    ParameterDefinition(
        name="min_brightness",
        label="Minimum brightness",
        hint=(
            "Increase this when the light does not turn on at its lowest level. Smart "
            "sampling stays inside this range; pin min and max to the same value to "
            "measure only that brightness."
        ),
        group=RANGE_BOUNDS,
        axis=ParameterAxis.BRIGHTNESS,
    ),
    ParameterDefinition(
        name="max_brightness",
        label="Maximum brightness",
        hint=(
            "Lower this when the light's top end is unused or unstable. Smart sampling "
            "does not invent points above this, even when a seed LUT already has them."
        ),
        group=RANGE_BOUNDS,
        axis=ParameterAxis.BRIGHTNESS,
    ),
    ParameterDefinition(
        name="min_kelvin",
        label="Minimum color temperature (K)",
        hint="Warmer end of the range. Lower Kelvin is warmer.",
        group=RANGE_BOUNDS,
        axis=ParameterAxis.KELVIN,
    ),
    ParameterDefinition(
        name="max_kelvin",
        label="Maximum color temperature (K)",
        hint="Cooler end of the range. Higher Kelvin is cooler.",
        group=RANGE_BOUNDS,
        axis=ParameterAxis.KELVIN,
    ),
    ParameterDefinition(
        name="min_sat",
        label="Minimum saturation",
        group=RANGE_BOUNDS,
        axis=ParameterAxis.SAT,
    ),
    ParameterDefinition(
        name="max_sat",
        label="Maximum saturation",
        group=RANGE_BOUNDS,
        axis=ParameterAxis.SAT,
    ),
    ParameterDefinition(
        name="min_hue",
        label="Minimum hue",
        group=RANGE_BOUNDS,
        axis=ParameterAxis.HUE,
    ),
    ParameterDefinition(
        name="max_hue",
        label="Maximum hue",
        group=RANGE_BOUNDS,
        axis=ParameterAxis.HUE,
    ),
    ParameterDefinition(
        name="smart_sampling",
        label="Smart envelope sampling",
        hint=(
            "Discover the light's highest and lowest power curves, then fill the band "
            "between them at a constant plot spacing. Replaces the cartesian CT/HS "
            "sweep product while this is on."
        ),
        group=RESOLUTION,
        control=ParameterControl.BOOLEAN,
    ),
    ParameterDefinition(
        name="smart_delta",
        label="Interior spacing (Δ)",
        hint=(
            "Distance in a square whose X is brightness 0–100% and Y is watts as "
            "0–100% of the measured peak. Δ=8 is 8% of the brightness span "
            "(~20 steps on 1–255) and 8% of peak watts (~0.4 W on a 5 W lamp). "
            "Two samples cover each other when the hypotenuse of those two "
            "percentages is less than Δ. Interior fill walks each color rail "
            "separately — a dense curve of one color does not fill holes of another. "
            "Smaller packs more densely."
        ),
        step="0.5",
        group=RESOLUTION,
    ),
    ParameterDefinition(
        name="smart_border_delta",
        label="Border spacing (Δ)",
        hint=(
            "Equidistant spacing along the envelope border: the 100% color sweep "
            "and the hottest/coldest brightness rails. Smaller traces those 1D "
            "edges more densely."
        ),
        step="0.5",
        group=RESOLUTION,
    ),
    ParameterDefinition(
        name="smart_dart",
        label="Dart-throw fill",
        hint=(
            "After the 100% outline and brightness rails, throw random interior "
            "samples biased toward high power. A throw whose guessed plot position "
            "is too close to an existing point is skipped. Spacing starts at "
            "interior Δ and shrinks toward the floor so a long run keeps adding "
            "detail. Cancel when you have enough."
        ),
        group=RESOLUTION,
        control=ParameterControl.BOOLEAN,
    ),
    ParameterDefinition(
        name="smart_dart_min_delta",
        label="Dart floor (Δ)",
        hint=(
            "Smallest dart radius. When a throw round accepts nothing, the radius "
            "shrinks toward this value. At the floor, an empty round ends the run."
        ),
        step="0.5",
        group=RESOLUTION,
    ),
    ParameterDefinition(
        name="bri_bri_steps",
        label="Brightness mode step",
        hint="Native brightness increment. Check Bisect to treat this as a sweep count instead.",
        group=RESOLUTION,
        bisection="bri_bri_bisection",
        all_values="bri_bri_all",
        sweep_label="Brightness mode sweeps",
        axis=ParameterAxis.BRIGHTNESS,
    ),
    ParameterDefinition(
        name="ct_bri_steps",
        label="Color temperature brightness step",
        hint=(
            "Native brightness increment while measuring color temperature. Smart: the "
            "step of the forced 0-100% ramps at the global max, both sides of a "
            "discontinuity, and prominent local extrema. Bisect turns this into a sweep "
            "count."
        ),
        group=RESOLUTION,
        bisection="ct_bri_bisection",
        all_values="ct_bri_all",
        sweep_label="Color temperature brightness sweeps",
        axis=ParameterAxis.BRIGHTNESS,
    ),
    ParameterDefinition(
        name="ct_mired_divisions",
        label="Color temperature sweeps",
        hint=(
            "How many color-temperature samples to take. Cartesian: number of brightness "
            "sweeps (1 = midpoint, 2 = min and max, …). Smart: the linear sweep at 100% "
            "brightness — this is the CT discovery density, not Interior/Border Δ. "
            "All walks every supported color temperature in the selected Kelvin range."
        ),
        group=RESOLUTION,
        all_values="ct_mired_all",
        axis=ParameterAxis.MIRED,
    ),
    ParameterDefinition(
        name="hs_bri_steps",
        label="HS brightness step",
        hint=(
            "Native brightness increment used for hue and saturation. Smart: the step of "
            "the forced 0-100% ramps at the RGB primaries and the midpoints between "
            "them, at min and max saturation. Defaults coarser than the CT step (~1/6 as "
            "many samples) because there are twelve of these ramps. Bisect turns this "
            "into a sweep count."
        ),
        group=RESOLUTION,
        bisection="hs_bri_bisection",
        all_values="hs_bri_all",
        sweep_label="HS brightness sweeps",
        axis=ParameterAxis.BRIGHTNESS,
    ),
    ParameterDefinition(
        name="hs_hue_divisions",
        label="HS hue sweeps",
        hint=(
            "How many distinct hues to sweep around the wheel. Must be a multiple of 3 "
            "(minimum 3): seeded at red/green/blue (an RGB(WW) fixture's actual "
            "emitters), then trisected. A full wheel is a ring, so 3/6/9 are 3/6/9 "
            "unique positions — the wrap-around red is not measured twice. All walks "
            "every hue; that is a huge grid."
        ),
        step="3",
        group=RESOLUTION,
        all_values="hs_hue_all",
        axis=ParameterAxis.HUE,
    ),
    ParameterDefinition(
        name="hs_sat_divisions",
        label="HS saturation sweeps",
        hint=(
            "How many saturation samples to take at 100% brightness. Cartesian: sweeps "
            "across the saturation range (1 = full saturation only, 2 = full then the "
            "midpoint, …). Smart: the same count on every point-of-interest hue — RGB "
            "primaries, the midpoints between them, and distant full-sat local maxima. "
            "Those are the vertical pillars on the hue-at-100% plot. All walks every "
            "saturation."
        ),
        group=RESOLUTION,
        all_values="hs_sat_all",
        axis=ParameterAxis.SAT,
    ),
    ParameterDefinition(
        name="effect_bri_steps",
        label="Effect brightness step",
        hint="Native brightness increment between long-running effect samples. Bisect turns this into a sweep count.",
        group=RESOLUTION,
        bisection="effect_bri_bisection",
        all_values="effect_bri_all",
        sweep_label="Effect brightness sweeps",
        axis=ParameterAxis.BRIGHTNESS,
    ),
    ParameterDefinition(
        name="brightness_descending",
        label="Sweep brightness high to low",
        hint=(
            "Walk each brightness rail high → low. Off walks low → high. "
            "A new run first measures min and max brightness on both color-temp ends "
            "and on each RGB primary so every LED emitter has a scale; "
            "resume and refine skip that."
        ),
        group=RESOLUTION,
        control=ParameterControl.BOOLEAN,
    ),
    ParameterDefinition(
        name="measure_time_effect_min",
        label="Minimum time per effect (seconds)",
        hint="An effect can stop after this time once its average converges.",
        group=RESOLUTION,
    ),
    ParameterDefinition(
        name="measure_time_effect",
        label="Maximum time per effect (seconds)",
        hint="Upper time limit for every effect and brightness combination.",
        group=RESOLUTION,
    ),
)

POWER_FIELD = FormFieldDefinition(
    name="power_entity_id",
    label="Power sensor",
    control=FieldControl.ENTITY,
    role=FieldRole.POWER_METER,
    entity_domains=("sensor",),
)

MEASUREMENT_REGISTRY: dict[MeasureType, MeasurementDefinition] = {
    MeasureType.LIGHT: MeasurementDefinition(
        measure_type=MeasureType.LIGHT,
        description="Build a lookup-table power profile for a light.",
        icon="💡",
        model_id_example="LWA017",
        product_name_example="Hue White Ambiance A60 E27",
        parameters=LIGHT_PARAMETERS,
        fields=(
            POWER_FIELD,
            _controller("light_entity_id", "Light", "light", multiple=True, plural_label="Lights"),
            MODES_FIELD,
            FormFieldDefinition(
                name="multiple_light_count",
                label="Number of lights",
                control=FieldControl.NUMBER,
                default=1,
                minimum=1,
                maximum=100,
                step="1",
                derived_from="light_entity_id",
                hint="Total number of identical physical lights; measured power is divided by this value.",
            ),
            FormFieldDefinition(
                name="rated_power_w",
                label="Rated power per light (W)",
                control=FieldControl.NUMBER,
                required=False,
                minimum=0,
                step="0.1",
                hint=(
                    "Optional lamp rating. Smart CT sweeps it as one extra guess "
                    "(weaker white faded in on top of the stronger, up to this budget) "
                    "— not treated as the real peak. Also bounds OCR meter readings "
                    "against gross misreads. Leave blank to skip both."
                ),
            ),
        ),
        supports_resume=True,
    ),
    MeasureType.SPEAKER: MeasurementDefinition(
        measure_type=MeasureType.SPEAKER,
        description="Measure power across media-player volume levels.",
        icon="🔊",
        model_id_example="B7W64E",
        product_name_example="Amazon Echo Dot (Gen4)",
        confirmation_action="Start speaker measurement",
        confirmation_is_warning=True,
        parameters=(
            READING_INTERVAL,
            ParameterDefinition(name="sleep_standby", label="Standby stabilization (seconds)", group=SAMPLING),
        ),
        fields=(
            POWER_FIELD,
            _controller("media_player_entity_id", "Media player", "media_player"),
            FormFieldDefinition(
                name="disable_streaming",
                label="Disable automatic pink-noise streaming",
                control=FieldControl.BOOLEAN,
                required=False,
                default=False,
            ),
        ),
    ),
    MeasureType.RECORDER: MeasurementDefinition(
        measure_type=MeasureType.RECORDER,
        description="Record power readings, optionally together with Home Assistant entity states.",
        icon="⏺",
        confirmation_action="Start recording",
        parameters=(READING_INTERVAL, *POINT_SAMPLING),
        fields=(
            POWER_FIELD,
            FormFieldDefinition(
                name="recorder_purpose",
                label="What do you want to create?",
                control=FieldControl.SELECT,
                options=(
                    FieldOption(
                        value=RecorderPurpose.PLAYBOOK,
                        label="A Playbook CSV",
                        description="Record power readings in the two-column format used to build a playbook.",
                    ),
                    FieldOption(
                        value=RecorderPurpose.COMPLEX_PROFILE,
                        label="Data for a complex power profile (experimental)",
                        description=(
                            "This experimental workflow records JSON Lines source data and can create a fixed "
                            "states_power model when one state or attribute clearly explains power. Composite models "
                            "are not supported yet, so the workflow is not feature complete. Hold every relevant "
                            "device state for at least five samples."
                        ),
                    ),
                ),
                default=RecorderPurpose.PLAYBOOK,
                review=True,
            ),
            FormFieldDefinition(
                name="profile_recipe",
                label="Device type",
                control=FieldControl.SELECT,
                options=(
                    FieldOption(
                        value=RecorderProfileRecipe.GENERIC,
                        label="Generic device",
                        description="Choose the entities whose states may explain changes in power.",
                    ),
                    FieldOption(
                        value=RecorderProfileRecipe.VACUUM_ROBOT,
                        label="Robot vacuum",
                        description="Capture the vacuum, its battery level, and optional dock or feature entities.",
                        guidance=(
                            "Measure the complete dock or base station at the wall outlet.",
                            "Include a low-battery charging cycle and idle and cleaning states.",
                            "Also capture washing, drying, and dust-emptying when the dock supports them.",
                        ),
                    ),
                ),
                default=RecorderProfileRecipe.GENERIC,
                visible_when=(("recorder_purpose", (RecorderPurpose.COMPLEX_PROFILE,)),),
                review=True,
            ),
            FormFieldDefinition(
                name="tracked_entity_ids",
                label="Tracked entity",
                plural_label="Tracked entities",
                control=FieldControl.ENTITY,
                multiple=True,
                all_entities=True,
                visible_when=(
                    ("recorder_purpose", (RecorderPurpose.COMPLEX_PROFILE,)),
                    ("profile_recipe", (RecorderProfileRecipe.GENERIC,)),
                ),
                hint="Select at least one entity whose state or attributes may explain the device's power use.",
                review=True,
            ),
            FormFieldDefinition(
                name="vacuum_entity_id",
                label="Vacuum",
                control=FieldControl.ENTITY,
                entity_domains=("vacuum",),
                all_entities=True,
                visible_when=(
                    ("recorder_purpose", (RecorderPurpose.COMPLEX_PROFILE,)),
                    ("profile_recipe", (RecorderProfileRecipe.VACUUM_ROBOT,)),
                ),
                review=True,
            ),
            FormFieldDefinition(
                name="battery_entity_id",
                label="Battery level sensor",
                control=FieldControl.ENTITY,
                entity_device_classes=("battery",),
                all_entities=True,
                related_to="vacuum_entity_id",
                same_device_only=True,
                visible_when=(
                    ("recorder_purpose", (RecorderPurpose.COMPLEX_PROFILE,)),
                    ("profile_recipe", (RecorderProfileRecipe.VACUUM_ROBOT,)),
                ),
                hint=(
                    "PowerCalc vacuum profiles require a numeric battery percentage sensor on the same Home Assistant "
                    "device."
                ),
                review=True,
            ),
            FormFieldDefinition(
                name="additional_entity_ids",
                label="Additional entity",
                plural_label="Additional entities (optional)",
                control=FieldControl.ENTITY,
                required=False,
                multiple=True,
                all_entities=True,
                related_to="vacuum_entity_id",
                visible_when=(
                    ("recorder_purpose", (RecorderPurpose.COMPLEX_PROFILE,)),
                    ("profile_recipe", (RecorderProfileRecipe.VACUUM_ROBOT,)),
                ),
                hint=(
                    "Entities from the vacuum's device are listed first. You can also choose an entity from elsewhere."
                ),
                review=True,
            ),
        ),
        supports_profile=False,
    ),
    MeasureType.AVERAGE: MeasurementDefinition(
        measure_type=MeasureType.AVERAGE,
        description="Measure average power for a fixed duration.",
        icon="📊",
        confirmation_action="Start averaging",
        parameters=(READING_INTERVAL,),
        fields=(
            POWER_FIELD,
            FormFieldDefinition(
                name="duration",
                label="Duration (seconds)",
                control=FieldControl.NUMBER,
                default=60,
                minimum=1,
                maximum=86_400,
                step="1",
            ),
        ),
        supports_profile=False,
    ),
    MeasureType.CHARGING: MeasurementDefinition(
        measure_type=MeasureType.CHARGING,
        description="Measure charging power against battery level.",
        icon="🔋",
        model_id_example="s6_maxv",
        product_name_example="Roborock S6 MaxV",
        confirmation_action="Start charging measurement",
        parameters=(READING_INTERVAL, *POINT_SAMPLING),
        fields=(
            POWER_FIELD,
            FormFieldDefinition(
                name="charging_device_type",
                label="Charging device type",
                control=FieldControl.SELECT,
                options=(
                    FieldOption(
                        value=ChargingDeviceType.VACUUM_ROBOT,
                        label="Vacuum robot",
                        entity_domain=charging_entity_domain(ChargingDeviceType.VACUUM_ROBOT),
                    ),
                    FieldOption(
                        value=ChargingDeviceType.LAWN_MOWER_ROBOT,
                        label="Lawn mower robot",
                        entity_domain=charging_entity_domain(ChargingDeviceType.LAWN_MOWER_ROBOT),
                    ),
                ),
            ),
            _controller(
                "charging_entity_id",
                "Charging device",
                *(charging_entity_domain(device_type) for device_type in ChargingDeviceType),
                narrowed_by="charging_device_type",
            ),
        ),
    ),
    MeasureType.FAN: MeasurementDefinition(
        measure_type=MeasureType.FAN,
        description="Measure fan power across percentage levels.",
        icon="🌀",
        model_id_example="TP07",
        product_name_example="Dyson Purifier Cool TP07",
        parameters=(READING_INTERVAL,),
        fields=(POWER_FIELD, _controller("fan_entity_id", "Fan", "fan")),
    ),
}


def measurement_definitions() -> tuple[MeasurementDefinition, ...]:
    return tuple(MEASUREMENT_REGISTRY.values())
