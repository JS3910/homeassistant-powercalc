from collections import deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
import logging
import math
from typing import Literal

from measure.controller.light.capabilities import clamp_mired_range
from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightInfo
from measure.runner.errors import RunnerError
from measure.tuning import MeasurementParameters

_LOGGER = logging.getLogger("measure")

ESTIMATED_IO_DELAY = 0.15
LIGHT_MODE_ORDER = (LutMode.BRIGHTNESS, LutMode.COLOR_TEMP, LutMode.HS, LutMode.EFFECT)
#: Modes that already sweep the full 0-100% brightness range at every point they measure,
#: making a standalone plain-brightness pass redundant alongside either of them -- see
#: `build_light_plan`.
_COLOR_MODES = (LutMode.COLOR_TEMP, LutMode.HS)


def modes_to_measure(modes: Collection[LutMode]) -> set[LutMode]:
    """Modes the runner will actually sweep.

    A standalone brightness pass is dropped once CT or HS is also requested — those
    already walk 0–100% brightness at every colour point.
    """

    mode_set = set(modes)
    if LutMode.BRIGHTNESS in mode_set and mode_set.intersection(_COLOR_MODES):
        return mode_set - {LutMode.BRIGHTNESS}
    return mode_set
#: HA hue is a 16-bit ring (0 and 65535 are the same red). A full-wheel min/max
#: pair is one colour, not two, so hue grids must not include both ends.
HUE_MODULO = 65_536
HUE_MIN = 1
HUE_MAX = 65_535

CSV_HEADERS = {
    LutMode.HS: ["bri", "hue", "sat", "watt"],
    LutMode.COLOR_TEMP: ["bri", "mired", "watt"],
    LutMode.BRIGHTNESS: ["bri", "watt"],
    LutMode.EFFECT: ["effect", "bri", "watt"],
}


@dataclass(frozen=True)
class Variation:
    bri: int

    def to_csv_row(self) -> list[str | int | float]:
        return [self.bri]

    @property
    def mode(self) -> LutMode:
        return LutMode.BRIGHTNESS


@dataclass(frozen=True)
class HsVariation(Variation):
    hue: int
    sat: int

    def to_csv_row(self) -> list[str | int | float]:
        return [self.bri, self.hue, self.sat]

    @property
    def mode(self) -> LutMode:
        return LutMode.HS


@dataclass(frozen=True)
class ColorTempVariation(Variation):
    ct: int

    def to_csv_row(self) -> list[str | int | float]:
        return [self.bri, self.ct]

    @property
    def mode(self) -> LutMode:
        return LutMode.COLOR_TEMP


@dataclass(frozen=True)
class EffectVariation(Variation):
    effect: str

    def to_csv_row(self) -> list[str | int | float]:
        return [self.effect, self.bri]

    def is_effect_changed(self, other_variation: EffectVariation) -> bool:
        return self.effect != other_variation.effect

    @property
    def mode(self) -> LutMode:
        return LutMode.EFFECT


@dataclass
class LightModePlan:
    mode: LutMode
    variations: list[Variation]


@dataclass
class LightMeasurementPlan:
    modes: list[LightModePlan]
    effects: list[str]

    @property
    def variations(self) -> list[Variation]:
        return [variation for mode in self.modes for variation in mode.variations]

    @property
    def variation_count(self) -> int:
        return sum(len(mode.variations) for mode in self.modes)

    def for_mode(self, mode: LutMode) -> LightModePlan:
        return next(mode_plan for mode_plan in self.modes if mode_plan.mode == mode)


def low_load_probe_variations(
    plan: LightMeasurementPlan,
    *,
    min_brightness: int | None = None,
) -> list[Variation]:
    """Select bounded, deterministic low-load points from a light measurement plan.

    ``min_brightness`` wins when the current plan only contains 100% rails (smart
    refine): a full-bright CT/HS sample is not a low-load reading.
    """

    probes: list[Variation] = []
    for mode_plan in plan.modes:
        variations = mode_plan.variations
        if not variations or mode_plan.mode == LutMode.EFFECT:
            continue
        planned_min = min(variation.bri for variation in variations)
        target_bri = planned_min if min_brightness is None else min(planned_min, min_brightness)
        if mode_plan.mode == LutMode.BRIGHTNESS:
            probes.append(Variation(target_bri))
            continue
        if mode_plan.mode == LutMode.COLOR_TEMP:
            ct_at_floor = [
                variation
                for variation in variations
                if isinstance(variation, ColorTempVariation) and variation.bri == target_bri
            ] or [variation for variation in variations if isinstance(variation, ColorTempVariation)]
            if not ct_at_floor:
                continue
            probes.extend(
                replace(candidate, bri=target_bri)
                for candidate in (
                    min(ct_at_floor, key=lambda variation: variation.ct),
                    max(ct_at_floor, key=lambda variation: variation.ct),
                )
            )
            continue
        if mode_plan.mode == LutMode.HS:
            hs_at_floor = [
                variation
                for variation in variations
                if isinstance(variation, HsVariation) and variation.bri == target_bri
            ] or [variation for variation in variations if isinstance(variation, HsVariation)]
            if not hs_at_floor:
                continue
            maximum_saturation = max(variation.sat for variation in hs_at_floor)
            hs_candidates = [variation for variation in hs_at_floor if variation.sat == maximum_saturation]
            probes.extend(
                replace(
                    min(hs_candidates, key=lambda variation: abs(variation.hue - primary_hue)),
                    bri=target_bri,
                )
                for primary_hue in _rgb_primary_hues()
            )

    return list(dict.fromkeys(probes))


def build_light_plan(
    modes: Collection[LutMode],
    parameters: MeasurementParameters,
    light_info: LightInfo,
    effects: Sequence[str] | None = None,
) -> LightMeasurementPlan:
    """Build the ordered variations used by preflight and runtime execution."""

    requested = set(modes)
    mode_set = modes_to_measure(modes)
    if LutMode.BRIGHTNESS in requested and mode_set != requested:
        # A plain-brightness pass measures whatever color/temp the light already happens
        # to be set to -- there is no "default" color or color temperature, so it isn't
        # holding anything meaningful constant. Once a color mode is also selected, that
        # mode's own sweep already covers the full brightness range at every color point
        # it measures, so the standalone pass adds nothing and is dropped. Confirmed
        # 2026-09-06: without this, a brightness+color_temp+HS request ran an initial
        # brightness pass against whatever color the light was last left in (observed:
        # solid blue, left over from a previous HS run), producing a LUT column that
        # doesn't correspond to any real, repeatable device state.
        _LOGGER.info(
            "Dropping the standalone brightness sweep: redundant once %s is also being measured",
            " and ".join(mode.value for mode in _COLOR_MODES if mode in mode_set),
        )
    effect_list = list(effects or [])
    return LightMeasurementPlan(
        modes=[
            LightModePlan(
                mode=mode,
                variations=_variations_for_mode(mode, parameters, light_info, effect_list),
            )
            for mode in LIGHT_MODE_ORDER
            if mode in mode_set
        ],
        effects=effect_list,
    )


def variation_from_csv_row(row: Sequence[str], mode: LutMode) -> Variation | None:
    """Parse a measurement CSV data row into its variation, or None when incomplete.

    A row only counts when every variation column parses and the power column holds
    a finite number, so torn rows from an interrupted write are never resumed from.
    """
    watt_index = len(CSV_HEADERS[mode]) - 1
    try:
        if len(row) <= watt_index or not math.isfinite(float(row[watt_index])):
            return None
        if mode == LutMode.BRIGHTNESS:
            return Variation(bri=int(row[0]))
        if mode == LutMode.COLOR_TEMP:
            return ColorTempVariation(bri=int(row[0]), ct=int(row[1]))
        if mode == LutMode.HS:
            return HsVariation(bri=int(row[0]), hue=int(row[1]), sat=int(row[2]))
        if mode == LutMode.EFFECT:
            return EffectVariation(effect=row[0], bri=int(row[1])) if row[0].strip() else None
    except ValueError:
        return None
    raise RunnerError(f"Mode {mode} not supported")


def variations_after(variations: Sequence[Variation], resume_at: Variation | None) -> list[Variation]:
    if resume_at is None:
        return list(variations)
    try:
        index = variations.index(resume_at)
    except ValueError:
        raise RunnerError(
            "The existing measurement CSV does not match the configured measurement grid; "
            "start a new session or restore the original settings to resume",
        ) from None
    return list(variations[index + 1 :])


def estimate_light_time_left(
    plan: LightMeasurementPlan,
    parameters: MeasurementParameters,
    *,
    current_mode: LutMode | None = None,
    remaining_variations: Sequence[Variation] | None = None,
    current_variation: Variation | None = None,
    extra_variations: int = 0,
    seconds_per_point: float | None = None,
) -> float:
    """Return the runner's remaining-time estimate in seconds."""

    all_variations = plan.variations
    remaining = list(remaining_variations) if remaining_variations is not None else all_variations
    if not all_variations:
        return 0

    mode = current_mode or plan.modes[0].mode
    progress = len(all_variations) - len(remaining)
    step_time = (
        seconds_per_point if seconds_per_point is not None and seconds_per_point > 0 else _step_time(mode, parameters)
    )

    time_left = 0.0
    if progress == 0:
        time_left += parameters.sleep_standby + parameters.sleep_initial
    time_left += (len(remaining) + max(0, extra_variations)) * step_time
    time_left += _mode_transition_time(mode, current_variation, plan.effects, parameters)

    remaining_modes = {variation.mode for variation in remaining}
    time_left += sum(
        _mode_transition_time(remaining_mode, None, plan.effects, parameters)
        for remaining_mode in remaining_modes
        if remaining_mode != mode
    )
    return max(0.0, time_left)


def color_temp_mireds(parameters: MeasurementParameters, light_info: LightInfo) -> list[int]:
    """CT axis points: the same grid as the cartesian mired sweep, ends first.

    Smart uses this as a linear 100% brightness pass. ``ct_mired_all`` walks every
    mired; otherwise ``ct_mired_divisions`` is the sweep count (1 = midpoint only).
    Pure cool and warm are always included so the WW/CW ends exist for the power-mix
    guess even when the user asked for a single midpoint sweep.
    """

    cool, warm = clamp_mired_range(light_info, parameters.min_kelvin, parameters.max_kelvin)
    if parameters.ct_mired_all:
        grid = [cool] if cool == warm else list(range(cool, warm + 1))
    else:
        grid = _divisions_range(parameters, cool, warm, parameters.ct_mired_divisions)
    ordered = list(dict.fromkeys((cool, warm, *grid)))
    if cool != warm and len(ordered) >= 2:
        rest = [mired for mired in ordered if mired not in (cool, warm)]
        return [cool, warm, *rest]
    return ordered


def hue_sweep_values(parameters: MeasurementParameters) -> list[int]:
    """Hue samples for the 100% HS outline — the same count as the cartesian hue sweep."""

    return _hue_grid(
        parameters,
        parameters.min_hue,
        parameters.max_hue,
        parameters.hs_hue_divisions,
        all_values=parameters.hs_hue_all,
    )


def sat_sweep_values(parameters: MeasurementParameters) -> list[int]:
    """Saturation samples at 100% brightness: outline primaries, then each POI hue."""

    return _saturation_sweep_values(
        parameters,
        parameters.min_sat,
        parameters.max_sat,
        parameters.hs_sat_divisions,
    )


def _variations_for_mode(
    mode: LutMode,
    parameters: MeasurementParameters,
    light_info: LightInfo,
    effects: list[str],
) -> list[Variation]:
    from measure.runner.smart_envelope import phase_a_variations, smart_applies

    if smart_applies(mode, parameters):
        return phase_a_variations(mode, parameters, light_info)
    axes = _mode_axis_values(mode, parameters, light_info, effects)
    if mode == LutMode.BRIGHTNESS:
        return [Variation(bri=bri) for bri in axes["brightness"]]
    if mode == LutMode.COLOR_TEMP:
        return [ColorTempVariation(bri=bri, ct=mired) for mired in axes["mired"] for bri in axes["brightness"]]
    if mode == LutMode.HS:
        return [
            HsVariation(bri=bri, hue=hue, sat=sat)
            for sat in axes["sat"]
            for hue in axes["hue"]
            for bri in axes["brightness"]
        ]
    if mode == LutMode.EFFECT:
        return [EffectVariation(bri=bri, effect=effect) for effect in axes["effect"] for bri in axes["brightness"]]
    raise RunnerError(f"Mode {mode} not supported")


def _mode_axis_values(
    mode: LutMode,
    parameters: MeasurementParameters,
    light_info: LightInfo,
    effects: Sequence[str],
) -> dict[str, list[int] | list[str]]:
    """Ordered values for each axis of a mode, without the cartesian product.

    Estimate uses the lengths so a hue-All grid is not materialised on every keystroke.
    """
    if mode == LutMode.BRIGHTNESS:
        return {
            "brightness": _brightness_values(
                parameters,
                parameters.min_brightness,
                parameters.max_brightness,
                parameters.bri_bri_steps,
                bisection=parameters.bri_bri_bisection,
                all_values=parameters.bri_bri_all,
            ),
        }
    if mode == LutMode.COLOR_TEMP:
        min_mired, max_mired = clamp_mired_range(light_info, parameters.min_kelvin, parameters.max_kelvin)
        # Warm/cold-white LEDs are physically two emitters blended by drive ratio, so the
        # two extremes are the most informative points; everything else only interpolates
        # between them. Sweep mired in bisection order (min, max, midpoint, then the
        # midpoints of the two halves, ...) so the profile's shape emerges quickly and only
        # gains resolution the longer the run is allowed to continue.
        return {
            "mired": _ordered_axis_values(
                parameters,
                min_mired,
                max_mired,
                parameters.ct_mired_divisions,
                all_values=parameters.ct_mired_all,
                order="bisection",
            ),
            "brightness": _brightness_values(
                parameters,
                parameters.min_brightness,
                parameters.max_brightness,
                parameters.ct_bri_steps,
                bisection=parameters.ct_bri_bisection,
                all_values=parameters.ct_bri_all,
            ),
        }
    if mode == LutMode.HS:
        # An RGB(WW) fixture is three emitters (red/green/blue) mixed by drive ratio, so
        # those three hues are the most informative points on the wheel; seed there first
        # (same three hues `low_load_probe_variations` above already singles out), then
        # trisect the gaps between them, wrapping around the circle.
        # Brightness must be the innermost/leaf sweep: power only really differs at the
        # higher end, and a 0-100% brightness sweep needs to complete for every (hue, sat)
        # combination. Confirmed 2026-09-06: the previous nesting interrupted brightness.
        return {
            "sat": _ordered_axis_values(
                parameters,
                parameters.min_sat,
                parameters.max_sat,
                parameters.hs_sat_divisions,
                all_values=parameters.hs_sat_all,
                order="saturation",
            ),
            "hue": _ordered_axis_values(
                parameters,
                parameters.min_hue,
                parameters.max_hue,
                parameters.hs_hue_divisions,
                all_values=parameters.hs_hue_all,
                order="trisection",
            ),
            "brightness": _brightness_values(
                parameters,
                parameters.min_brightness,
                parameters.max_brightness,
                parameters.hs_bri_steps,
                bisection=parameters.hs_bri_bisection,
                all_values=parameters.hs_bri_all,
            ),
        }
    if mode == LutMode.EFFECT:
        if not effects:
            raise RunnerError("No effects found for the light")
        return {
            "effect": list(effects),
            "brightness": _brightness_values(
                parameters,
                max(parameters.min_brightness, 5),
                parameters.max_brightness,
                parameters.effect_bri_steps,
                bisection=parameters.effect_bri_bisection,
                all_values=parameters.effect_bri_all,
            ),
        }
    raise RunnerError(f"Mode {mode} not supported")


def light_mode_axis_sizes(
    mode: LutMode,
    parameters: MeasurementParameters,
    light_info: LightInfo,
    effects: Sequence[str],
) -> list[tuple[str, int]]:
    """Axis sizes in the order the setup-page product string uses."""

    return [(name, len(values)) for name, values in _mode_axis_values(mode, parameters, light_info, effects).items()]


def summarize_light_modes(
    modes: Collection[LutMode],
    parameters: MeasurementParameters,
    light_info: LightInfo,
    effects: Sequence[str] | None = None,
) -> list[tuple[LutMode, list[tuple[str, int]], int, str]]:
    """Per-mode axis sizes and products, using the same grid as `build_light_plan`.

    Does not materialise the cartesian product, so hue All stays cheap to count.
    """

    mode_set = modes_to_measure(modes)
    effect_list = list(effects or [])
    summaries: list[tuple[LutMode, list[tuple[str, int]], int, str]] = []
    from measure.runner.smart_envelope import estimate_smart_mode, smart_applies

    for mode in LIGHT_MODE_ORDER:
        if mode not in mode_set:
            continue
        if smart_applies(mode, parameters):
            estimate = estimate_smart_mode(mode, parameters, light_info)
            axes = [("discovery", estimate.discovery), ("coverage_cap", estimate.coverage_cap)]
            summaries.append((mode, axes, estimate.discovery, estimate.summary))
            continue
        axes = light_mode_axis_sizes(mode, parameters, light_info, effect_list)
        points = 1
        for _, size in axes:
            points *= size
        summaries.append((mode, axes, points, _format_axis_product(axes, points)))
    return summaries


def estimate_light_run_seconds(
    mode_points: dict[LutMode, int],
    parameters: MeasurementParameters,
    effects: Sequence[str] | None = None,
) -> float:
    """Fresh-run duration matching `estimate_light_time_left` without building variations."""

    if not mode_points:
        return 0
    first_mode = next(mode for mode in LIGHT_MODE_ORDER if mode in mode_points)
    total = sum(mode_points.values())
    time_left = parameters.sleep_standby + parameters.sleep_initial
    time_left += total * _step_time(first_mode, parameters)
    effect_list = list(effects or [])
    time_left += _mode_transition_time(first_mode, None, effect_list, parameters)
    time_left += sum(
        _mode_transition_time(mode, None, effect_list, parameters) for mode in mode_points if mode != first_mode
    )
    return max(0.0, time_left)


_AXIS_DISPLAY_NAMES = {"mired": "kelvin"}


def _format_axis_product(axes: list[tuple[str, int]], points: int) -> str:
    if not axes:
        return f"0 = {points}"
    labeled = [(_AXIS_DISPLAY_NAMES.get(name, name), size) for name, size in axes]
    if len(labeled) == 1:
        name, size = labeled[0]
        return f"{size} {name} = {points}"
    return " × ".join(f"{size} {name}" for name, size in labeled) + f" = {points}"


def _brightness_values(
    parameters: MeasurementParameters,
    start: int,
    end: int,
    step_or_divisions: int,
    *,
    bisection: bool,
    all_values: bool,
) -> list[int]:
    """Brightness samples on one rail, walked min→max or max→min.

    Bisect/All choose *which* brightnesses sit on the rail (a sweep count, or
    every integer). They do not choose visit order — that is always linear so
    a rail is one sweep. Which rail runs next (mired, hue, saturation) is the
    outer bisection, so adjacent rails are not measured back-to-back.

    A brand-new session prepends emitter-bound diagnostics via
    ``premeasure_emitter_bounds``; this list itself stays monotonic.
    """
    if parameters.fast_test_mode:
        values = [start] if start == end else [start, end]
    elif all_values:
        values = list(range(min(start, end), max(start, end) + 1))
    elif bisection:
        values = _divisions_range(parameters, start, end, step_or_divisions)
    else:
        values = _measurement_range(parameters, start, end, step_or_divisions)
    values = sorted(set(values))
    return list(reversed(values)) if parameters.brightness_descending else values


def _hue_distance(left: int, right: int) -> int:
    delta = abs(left - right) % HUE_MODULO
    return min(delta, HUE_MODULO - delta)


def _rgb_primary_hues() -> tuple[int, int, int]:
    """Red / green / blue on HA's 16-bit hue ring (the three RGB emitters)."""

    return (HUE_MIN, HUE_MODULO // 3, (2 * HUE_MODULO) // 3)


def _emitter_diagnostic_variations(variations: Sequence[Variation]) -> list[Variation]:
    """Min and max brightness on each physical emitter this mode can isolate."""

    if not variations:
        return []
    min_bri = min(variation.bri for variation in variations)
    max_bri = max(variation.bri for variation in variations)
    if min_bri == max_bri:
        return []
    first = variations[0]
    if isinstance(first, ColorTempVariation):
        cts = [variation.ct for variation in variations if isinstance(variation, ColorTempVariation)]
        corners = [ColorTempVariation(bri=bri, ct=ct) for ct in (min(cts), max(cts)) for bri in (max_bri, min_bri)]
        return list(dict.fromkeys(corners))
    if isinstance(first, HsVariation):
        hs_rows = [variation for variation in variations if isinstance(variation, HsVariation)]
        max_sat = max(variation.sat for variation in hs_rows)
        at_full_sat = [variation for variation in hs_rows if variation.sat == max_sat]
        if not at_full_sat:
            return []
        corners: list[Variation] = []
        for primary_hue in _rgb_primary_hues():
            nearest = min(at_full_sat, key=lambda variation: _hue_distance(variation.hue, primary_hue))
            corners.extend(replace(nearest, bri=bri) for bri in (max_bri, min_bri))
        return list(dict.fromkeys(corners))
    if isinstance(first, EffectVariation):
        return []
    return [Variation(max_bri), Variation(min_bri)]


def premeasure_emitter_bounds(variations: Sequence[Variation]) -> list[Variation]:
    """On a brand-new run, measure each LED emitter at min and max brightness.

    Color-temp: both kelvin ends (warm / cold white). HS: each RGB primary at
    full saturation. Brightness-only: min and max. Those points are pulled to
    the front and dropped from the later linear walk so each is recorded once.
    Resume and extend skip this — they already have emitter bounds on disk.
    """
    diagnostics = _emitter_diagnostic_variations(variations)
    if not diagnostics:
        return list(variations)
    taken = set(diagnostics)
    return [*diagnostics, *(variation for variation in variations if variation not in taken)]


def _ordered_axis_values(
    parameters: MeasurementParameters,
    start: int,
    end: int,
    divisions: int,
    *,
    all_values: bool,
    order: Literal["bisection", "trisection", "saturation"],
) -> list[int]:
    """Sweep-count axis, or every integer in the live range when All is on."""

    if order == "saturation" and not all_values:
        return _saturation_sweep_values(parameters, start, end, divisions)
    if order == "trisection":
        return _circular_trisection_order(_hue_grid(parameters, start, end, divisions, all_values=all_values))
    if parameters.fast_test_mode:
        grid = [start] if start == end else [start, end]
    elif all_values:
        grid = list(range(min(start, end), max(start, end) + 1))
    else:
        grid = _divisions_range(parameters, start, end, divisions)
    if order == "bisection":
        return _bisection_order(grid)
    return _saturation_order(grid)


def _hue_grid(
    parameters: MeasurementParameters,
    start: int,
    end: int,
    divisions: int,
    *,
    all_values: bool,
) -> list[int]:
    """Hue values for a sweep count, or every integer when All is on.

    A full wheel is a ring: ``N`` configured sweeps are ``N`` distinct hues, never
    both wrap-around reds (1 and 65535). A partial arc stays a linear inclusive
    range, where the two ends are different colours.
    """

    if parameters.fast_test_mode:
        if start == end:
            return [start]
        if _is_full_hue_circle(start, end):
            opposite = _wrap_hue(start + HUE_MODULO // 2)
            return [start] if opposite == start else [start, opposite]
        return [start, end]
    if all_values:
        low, high = min(start, end), max(start, end)
        if _is_full_hue_circle(low, high):
            return list(range(low, high))
        return list(range(low, high + 1))
    if start == end:
        return [start]
    if _is_full_hue_circle(start, end):
        return _circular_hue_values(start, divisions)
    return _divisions_range(parameters, start, end, divisions)


def _is_full_hue_circle(start: int, end: int) -> bool:
    """True when min/max meet around the 16-bit wrap, so they are one colour."""

    return abs(end - start) >= HUE_MODULO - 2


def _wrap_hue(value: int) -> int:
    hue = value % HUE_MODULO
    return HUE_MAX if hue < HUE_MIN else hue


def _circular_hue_values(start: int, divisions: int) -> list[int]:
    """``divisions`` unique hues around the wheel, starting at ``start``."""

    divisions = max(1, divisions)
    return list(dict.fromkeys(_wrap_hue(start + round(i * HUE_MODULO / divisions)) for i in range(divisions)))


def _inclusive_range(start: int, end: int, step: int) -> list[int]:
    values = list(range(start, end, step))
    values.append(end)
    return values


def _measurement_range(parameters: MeasurementParameters, start: int, end: int, step: int) -> list[int]:
    if parameters.fast_test_mode:
        return [start] if start == end else [start, end]
    return _inclusive_range(start, end, step)


def _saturation_sweep_values(
    parameters: MeasurementParameters,
    start: int,
    end: int,
    divisions: int,
) -> list[int]:
    """Saturation points for a given sweep count, already in run order.

    Unlike mired/hue, one sweep is the *maximum* (pure color, the actual LED primary),
    not the midpoint. Two sweeps add the midpoint rather than the white end: min
    saturation is nearly a brightness-only white point, already covered by the
    brightness sweep. Three and above use even spacing across the full range (so the
    unsaturated end is included) and then `_saturation_order`.
    """
    if parameters.fast_test_mode:
        return [start] if start == end else [end, start]
    divisions = max(1, divisions)
    if start == end:
        return [start]
    if divisions == 1:
        return [end]
    if divisions == 2:
        mid = round((start + end) / 2)
        return _saturation_order([end] if mid == end else [mid, end])
    return _saturation_order(_divisions_range(parameters, start, end, divisions))


def _saturation_order(values: list[int]) -> list[int]:
    """Full saturation, then midpoint, then the unsaturated end, then bisection of the rest."""
    unique = sorted(set(values))
    if len(unique) <= 1:
        return unique
    low, high = unique[0], unique[-1]
    target = (low + high) / 2
    mid = min(unique, key=lambda value: (abs(value - target), -value))
    head = [high]
    if mid != high:
        head.append(mid)
    if low not in head:
        head.append(low)
    remaining = [value for value in _bisection_order(unique) if value not in head]
    return [*head, *remaining]


def _divisions_range(parameters: MeasurementParameters, start: int, end: int, divisions: int) -> list[int]:
    """Like `_measurement_range`, but the caller picks how many *sweeps* to divide the
    range into rather than a native step size: 1 sweep is just the midpoint, 2 is min and
    max, 3 is min/mid/max, and so on -- min and max only enter the result once there are
    at least 2 sweeps to place them at.

    Used for linear sweep-count axes (color-temp mired, a partial hue arc). A full
    hue wheel is a ring and goes through `_circular_hue_values` instead, so the
    wrap-around red is not counted twice.
    """
    if parameters.fast_test_mode:
        return [start] if start == end else [start, end]
    divisions = max(1, divisions)
    if start == end:
        return [start]
    if divisions == 1:
        return [round((start + end) / 2)]
    step = max(1, round((end - start) / (divisions - 1)))
    return _inclusive_range(start, end, step)


def _bisection_order(values: list[int]) -> list[int]:
    """Reorder a sorted line (e.g. mired) so it builds shape fast: both ends first, then
    the midpoint of the whole range, then the midpoints of each remaining half, and so on
    (breadth-first, so accuracy improves evenly across the range the longer this runs).
    """
    n = len(values)
    if n <= 2:
        return list(values)
    result = [values[0], values[-1]]
    seen = {0, n - 1}
    queue: deque[tuple[int, int]] = deque([(0, n - 1)])
    while queue:
        lo, hi = queue.popleft()
        if hi - lo <= 1:
            continue
        mid = (lo + hi) // 2
        if mid not in seen:
            result.append(values[mid])
            seen.add(mid)
        queue.append((lo, mid))
        queue.append((mid, hi))
    return result


def _circular_trisection_order(values: list[int], seed_count: int = 3) -> list[int]:
    """Reorder a sorted ring (hue, 0..65535 wrapping) so it builds shape fast: `seed_count`
    evenly spaced seeds first (the red/green/blue primaries an RGB(WW) fixture is actually
    built from, for the default of 3), then breadth-first *trisection* of the gaps between
    consecutive seeds, wrapping the last gap back to the first.

    Trisecting, not bisecting, so `hs_hue_divisions` counts (validated as multiples of 3 --
    see `LightMeasurementRequest.validate_light_parameters`) evenly distribute around the
    wheel at every level: a bisected count would keep doubling away from the 3-fold R/G/B
    symmetry the seeds start from, landing new points off-center between arcs that no
    longer line up with the fixture's actual emitters.
    """
    n = len(values)
    if n <= seed_count:
        return list(values)
    seed_indices = sorted({round(i * n / seed_count) % n for i in range(seed_count)})
    result = [values[index] for index in seed_indices]
    seen = set(seed_indices)
    wrapped_indices = [*seed_indices, seed_indices[0] + n]
    queue: deque[tuple[int, int]] = deque(pairwise(wrapped_indices))
    while queue:
        lo, hi = queue.popleft()
        span = hi - lo
        if span <= 1:
            continue
        thirds = sorted({lo + round(span / 3), lo + round(2 * span / 3)})
        for third in thirds:
            index = third % n
            if lo < third < hi and index not in seen:
                result.append(values[index])
                seen.add(index)
        boundaries = [lo, *thirds, hi]
        queue.extend(pair for pair in pairwise(boundaries) if pair[1] > pair[0])
    return result


def _step_time(mode: LutMode, parameters: MeasurementParameters) -> float:
    if mode == LutMode.EFFECT:
        return parameters.measure_time_effect + ESTIMATED_IO_DELAY
    step_time = parameters.sleep_time + ESTIMATED_IO_DELAY
    if parameters.settle_tolerance_pct > 0:
        step_time += parameters.settle_min_wait
    if parameters.sample_count > 1:
        step_time += parameters.sample_count * (parameters.sleep_time_sample + ESTIMATED_IO_DELAY)
    return step_time


def _mode_transition_time(
    mode: LutMode,
    current_variation: Variation | None,
    effects: list[str],
    parameters: MeasurementParameters,
) -> float:
    if mode == LutMode.EFFECT:
        effect_variation = current_variation if isinstance(current_variation, EffectVariation) else None
        effect_progress = effects.index(effect_variation.effect) if effect_variation else 0
        return (len(effects) - effect_progress - 1) * parameters.sleep_time_effect_change
    # Settle already covers color jumps; these extras only apply to fixed-sleep runs.
    if parameters.settle_tolerance_pct > 0:
        return 0
    if mode == LutMode.HS:
        hs_variation = current_variation if isinstance(current_variation, HsVariation) else None
        brightness = hs_variation.bri if hs_variation else parameters.min_brightness
        # Brightness is the innermost/fast leaf sweep now (see _variations_for_mode), hue
        # is the middle sweep and saturation is outermost. How far along the current
        # brightness sweep we are stands in for how many more hue changes are left in the
        # current saturation level (one hue change per finished brightness sweep), the
        # same proxy role brightness progress played for the axis one level up before this
        # was reordered -- still an approximation, not an exact index into either sweep's
        # (bisection-ordered, for hue) actual remaining order.
        hue_steps_left = max(
            0,
            round((parameters.max_brightness - brightness) / parameters.hs_bri_steps) - 1,
        )
        time_left = hue_steps_left * parameters.sleep_time_hue
        sat_steps_left = round(hue_steps_left / max(1, parameters.hs_hue_divisions))
        return time_left + sat_steps_left * parameters.sleep_time_sat
    if mode == LutMode.COLOR_TEMP:
        ct_variation = current_variation if isinstance(current_variation, ColorTempVariation) else None
        brightness = ct_variation.bri if ct_variation else parameters.min_brightness
        ct_steps_left = (
            round(
                (parameters.max_brightness - brightness) / parameters.ct_bri_steps,
            )
            - 1
        )
        return ct_steps_left * parameters.sleep_time_ct
    return 0
