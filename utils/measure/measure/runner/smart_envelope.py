"""Smart envelope sampling: discover the power band, then cover it in plot space.

Brightness is x (0-100 across the live span). Watts are y, normalized to 0-100 of
the measured peak, so Δ is one spacing in that plane. Powercalc still gets a
normal LUT; this module only chooses which rows to measure.

CT starts with a linear 100% brightness mired sweep (``ct_mired_divisions``),
then forced 0-255 brightness ramps (``ct_bri_steps``) at both sides of any
WW/CW cliff, the max and min of each isolated segment, and prominent local
extrema. HS starts with a 100% outline: a full-sat hue ring
(``hs_hue_divisions``), white, and representative saturations
(``hs_sat_divisions``) on the RGB primaries. After the ring exists, every
point-of-interest hue (primaries, midpoints, and distant full-sat local
maxima) gets the same saturation sweep at 100% brightness — those are the
vertical pillars on the hue-at-100% plot. Then forced brightness ramps
(``hs_bri_steps``) at min and max saturation on those hues. Coverage
then either packs a finite interior grid or,
when dart fill is on, throws high-power-weighted random points whose guessed
plot position is at least the current radius from every sample. The dart radius
starts at ``smart_delta`` and shrinks toward ``smart_dart_min_delta``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
from itertools import pairwise
import math
import random
import struct

from measure.controller.light.capabilities import clamp_mired_range
from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightInfo
from measure.runner.light_plan import (
    HUE_MAX,
    HUE_MIN,
    HUE_MODULO,
    ColorTempVariation,
    HsVariation,
    Variation,
    _brightness_values,
    color_temp_mireds,
    hue_sweep_values,
    sat_sweep_values,
)
from measure.tuning import MeasurementParameters

#: Default plot-space spacing. 8 is ~8% of the brightness span and ~8% of peak watts.
DEFAULT_SMART_DELTA = 8.0
#: Conservative y-width (normalized 0-100) when no peak is known yet.
_ESTIMATE_BAND_WIDTH = 50.0
#: How many CT rails the pre-run estimate assumes will get a forced brightness walk.
_FORCED_CT_RAMP_GUESS = 4
#: Isolated 100% samples this far from both neighbors (plot-y) are outliers, not extrema.
_ISOLATED_SPIKE_PLOT_Y = 20.0
_ISOLATED_NEIGHBOR_PLOT_Y = 4.0

_COLOR_MODES = (LutMode.COLOR_TEMP, LutMode.HS)


@dataclass(frozen=True)
class MeasuredPoint:
    variation: Variation
    watt: float


@dataclass(frozen=True)
class SmartEstimate:
    discovery: int
    coverage_cap: int
    summary: str


def smart_applies(mode: LutMode, parameters: MeasurementParameters) -> bool:
    return bool(parameters.smart_sampling) and mode in _COLOR_MODES


def smart_delta(parameters: MeasurementParameters) -> float:
    return max(parameters.smart_delta, 1.0)


def smart_border_delta(parameters: MeasurementParameters) -> float:
    return max(parameters.smart_border_delta, 1.0)


def smart_dart_floor(parameters: MeasurementParameters) -> float:
    return max(parameters.smart_dart_min_delta, 1.0)


def color_key(variation: Variation) -> tuple[object, ...]:
    if isinstance(variation, ColorTempVariation):
        return ("ct", variation.ct)
    if isinstance(variation, HsVariation):
        return ("hs", variation.hue, variation.sat)
    return ("bri", variation.bri)


def with_brightness(variation: Variation, bri: int) -> Variation:
    if isinstance(variation, ColorTempVariation):
        return ColorTempVariation(bri=bri, ct=variation.ct)
    if isinstance(variation, HsVariation):
        return HsVariation(bri=bri, hue=variation.hue, sat=variation.sat)
    return Variation(bri=bri)


def phase_a_variations(
    mode: LutMode,
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> list[Variation]:
    """CT: linear 100% mired sweep. HS: 100% hue ring, white, and sat samples on primaries."""

    if mode == LutMode.COLOR_TEMP:
        max_bri = parameters.max_brightness
        return [ColorTempVariation(bri=max_bri, ct=mired) for mired in color_temp_mireds(parameters, light_info)]
    return _hs_outline(parameters)


def estimate_smart_mode(
    mode: LutMode,
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> SmartEstimate:
    """Discovery count plus a conservative coverage cap from band area / Δ²."""

    discovery = (
        len(phase_a_variations(mode, parameters, light_info))
        + _forced_ramp_estimate(mode, parameters)
        + _poi_sat_estimate(mode, parameters)
    )
    if parameters.min_brightness == parameters.max_brightness:
        # No brightness axis to walk or fill. The coverage-cap formula assumes a
        # full 0-100% span and would invent hundreds of "remaining" points.
        pinned = parameters.max_brightness
        return SmartEstimate(
            discovery=discovery,
            coverage_cap=0,
            summary=f"discovery {discovery}, brightness pinned at {pinned}",
        )
    delta = smart_delta(parameters)
    border = smart_border_delta(parameters)
    perimeter = math.ceil(200.0 / border)
    if parameters.smart_dart:
        floor = min(smart_dart_floor(parameters), delta)
        interior = math.ceil((0.5 * 100.0 * _ESTIMATE_BAND_WIDTH) / (floor**2))
        coverage_cap = max(0, interior + perimeter)
        summary = f"discovery {discovery}, forced ramps, then dart fill (Δ {delta:g}→{floor:g}, ≤ {coverage_cap})"
        return SmartEstimate(discovery=discovery, coverage_cap=coverage_cap, summary=summary)
    interior = math.ceil((0.5 * 100.0 * _ESTIMATE_BAND_WIDTH) / (delta**2))
    coverage_cap = max(0, interior + perimeter)
    summary = f"discovery {discovery}, forced ramps, then coverage ≤ {coverage_cap}"
    return SmartEstimate(discovery=discovery, coverage_cap=coverage_cap, summary=summary)


def expected_smart_points(mode: LutMode, parameters: MeasurementParameters, light_info: LightInfo) -> int:
    estimate = estimate_smart_mode(mode, parameters, light_info)
    return estimate.discovery + estimate.coverage_cap


def smart_planner_has_work(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> bool:
    """Whether ``next_smart_batch`` would queue any key the seed does not already have."""

    known = {point.variation for point in measured}
    return any(point not in known for point in next_smart_batch(mode, measured, parameters, light_info).variations)


def estimate_smart_remaining(
    mode: LutMode,
    parameters: MeasurementParameters,
    light_info: LightInfo,
    measured: Collection[Variation],
    *,
    measured_points: Sequence[MeasuredPoint] | None = None,
) -> int:
    """How many smart points this mode still expects, given keys already on disk.

    The plan only queues the 100% outline; ramps and coverage are invented at
    runtime. Counting missing outline keys therefore reports 0 once the seed
    already has that sweep, even though the runner will still append batches.
    When watts are available, ask the planner: a dense seed that already covers
    this Δ has nothing left, and the coverage cap must not invent work.
    """

    if not smart_applies(mode, parameters):
        return 0
    if measured_points is not None and not smart_planner_has_work(
        mode,
        measured_points,
        parameters,
        light_info,
    ):
        return 0
    estimate = estimate_smart_mode(mode, parameters, light_info)
    outline = phase_a_variations(mode, parameters, light_info)
    known = {point for point in measured if point.mode == mode}
    missing_outline = sum(1 for point in outline if point not in known)
    # Seed extras (previous ramps/coverage) do not retire this plan. Forced walks and
    # coverage are invented from the current envelope and step size; a dense parent
    # LUT can have thousands of keys and still need those walks.
    extra_cap = max(0, estimate.discovery + estimate.coverage_cap - len(outline))
    return missing_outline + extra_cap


def smart_uninvented_count(
    modes: Collection[LutMode],
    all_variations: Sequence[Variation],
    remaining_variations: Sequence[Variation],
    parameters: MeasurementParameters,
    light_info: LightInfo,
    measured_by_mode: Mapping[LutMode, Sequence[MeasuredPoint]] | None = None,
) -> int:
    """Points the planner is expected to invent beyond what is already queued.

    After one smart mode finishes, later modes use that mode's actual size as a
    prior. The pre-run coverage cap is only a guess; the sibling mode is the same
    lamp, Δ, and planner.
    """

    if not parameters.smart_sampling:
        return 0
    planned = Counter(variation.mode for variation in all_variations)
    left = Counter(variation.mode for variation in remaining_variations)
    finished = [
        planned[mode] for mode in modes if smart_applies(mode, parameters) and planned[mode] > 0 and left[mode] == 0
    ]
    peer = int(sum(finished) / len(finished)) if finished else 0
    extra = 0
    for mode in modes:
        if not smart_applies(mode, parameters):
            continue
        if planned[mode] > 0 and left[mode] == 0:
            continue
        if planned[mode] == 0:
            seed = list((measured_by_mode or {}).get(mode, ()))
            if seed and not smart_planner_has_work(mode, seed, parameters, light_info):
                continue
        expected = max(expected_smart_points(mode, parameters, light_info), peer)
        extra += max(0, expected - planned[mode])
    return extra


STAGE_DISCOVERY = "Discovering envelope"
STAGE_COVERAGE = "Covering interior"
STAGE_DART = "Throwing darts"

REASON_HS_OUTLINE = (
    "Hardcoded 100% HS outline: hue ring, white, and primary saturations "
    "(hs_hue_divisions / hs_sat_divisions) — not chosen from measured power."
)
REASON_CT_SWEEP = (
    "Hardcoded 100% CT sweep across the mired range (ct_mired_divisions) — not chosen from measured power."
)
REASON_POST_SCOUT = "Extra 100% colors after the outline: a WW/CW mix guess or mid-saturation samples."
REASON_WATT_GAPS = "The 100% outline jumped more than Δ in watts; sampling the missing hue or CT in between."
REASON_FORCED_RAMPS = "Forced brightness walk"
REASON_POI_SAT = "Saturation sweep at 100%"
REASON_ENVELOPE_ENDS = "Min and max brightness on each color that forms the power envelope."
REASON_PERIMETER = "Tracing the envelope border at the configured border Δ."
REASON_COVERAGE = "Filling gaps of at least Δ along each color rail in brightness-watt plot space."
REASON_DART = (
    "Random high-power-weighted fill; each dart is at least the current radius from every sample already taken."
)


@dataclass(frozen=True)
class SmartBatch:
    variations: list[Variation]
    stage: str
    reason: str


@dataclass(frozen=True)
class _ForcedTarget:
    color: Variation
    reason: str


def next_smart_variations(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> list[Variation]:
    """Unmeasured variations for the next append, or empty when the band is covered."""

    return next_smart_batch(mode, measured, parameters, light_info).variations


def replay_smart_remaining(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> list[Variation]:
    """Replay planner batches using CSV rows as answers until one batch is incomplete.

    Smart invents rows after the initial sweep. Resume cannot treat the last CSV row
    as an index into that sweep. Walking each batch from only the points already on
    disk reconstructs the same remaining queue the interrupted run had, including
    mid-rail points that were never on the original 100% grid.
    """

    if not smart_applies(mode, parameters):
        return []
    known = {point.variation: point for point in measured}
    simulated: list[MeasuredPoint] = []
    answered: set[Variation] = set()
    for _ in range(512):
        batch = next_smart_batch(mode, simulated, parameters, light_info)
        if not batch.variations:
            return []
        remaining: list[Variation] = []
        for variation in batch.variations:
            existing = known.get(variation)
            if existing is None:
                remaining.append(variation)
                continue
            if variation not in answered:
                simulated.append(existing)
                answered.add(variation)
        if remaining:
            return remaining
    return []


def next_smart_batch(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> SmartBatch:
    """Next append and whether it is still discovery or interior coverage."""

    if not smart_applies(mode, parameters):
        return SmartBatch([], STAGE_DISCOVERY, REASON_HS_OUTLINE)
    known = {point.variation for point in measured}
    pending = [point for point in phase_a_variations(mode, parameters, light_info) if point not in known]
    if pending:
        outline = REASON_HS_OUTLINE if mode == LutMode.HS else REASON_CT_SWEEP
        return SmartBatch(pending, STAGE_DISCOVERY, outline)
    if not measured:
        return SmartBatch([], STAGE_DISCOVERY, REASON_HS_OUTLINE)

    discovery = _post_outline_discovery(mode, measured, parameters, light_info, known)
    if discovery is not None:
        return discovery
    envelope_colors = _rail_colors(mode, measured, parameters, light_info)
    if not envelope_colors:
        return SmartBatch([], STAGE_DISCOVERY, REASON_HS_OUTLINE)
    if parameters.smart_dart:
        return SmartBatch(
            _dart_coverage(mode, envelope_colors, measured, parameters, light_info, known),
            STAGE_DART,
            REASON_DART,
        )
    return SmartBatch(
        _coverage_variations(envelope_colors, measured, parameters, known),
        STAGE_COVERAGE,
        REASON_COVERAGE,
    )


def _post_outline_discovery(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
    known: set[Variation],
) -> SmartBatch | None:
    extras = _unmeasured(
        (
            with_brightness(color, parameters.max_brightness)
            for color in _post_scout_colors(mode, measured, parameters, light_info)
        ),
        known,
    )
    if extras:
        return SmartBatch(extras, STAGE_DISCOVERY, REASON_POST_SCOUT)
    gaps = _unmeasured(_max_bri_watt_gaps(mode, measured, parameters), known)
    if gaps:
        return SmartBatch(gaps, STAGE_DISCOVERY, REASON_WATT_GAPS)
    poi_sats = _next_in_range_poi_sat_batch(mode, measured, parameters, known)
    if poi_sats:
        return poi_sats
    walk = _brightness_walk(parameters, hs=mode == LutMode.HS)
    for target in _forced_targets(mode, measured, parameters):
        ramps = _unmeasured((with_brightness(target.color, bri) for bri in walk), known)
        if ramps:
            return SmartBatch(ramps, STAGE_DISCOVERY, f"{REASON_FORCED_RAMPS}: {target.reason}.")
    envelope_colors = _rail_colors(mode, measured, parameters, light_info)
    if not envelope_colors:
        return None
    ends = (parameters.max_brightness, parameters.min_brightness)
    followups = _unmeasured(
        (with_brightness(color, bri) for color in envelope_colors for bri in ends),
        known,
    )
    if followups:
        return SmartBatch(followups, STAGE_DISCOVERY, REASON_ENVELOPE_ENDS)
    perimeter = _unmeasured(_perimeter_trace(envelope_colors, measured, parameters), known)
    if perimeter:
        return SmartBatch(perimeter, STAGE_DISCOVERY, REASON_PERIMETER)
    return None


def _unmeasured(candidates: Iterable[Variation], known: set[Variation]) -> list[Variation]:
    extra: list[Variation] = []
    seen = set(known)
    for point in candidates:
        if point not in seen:
            seen.add(point)
            extra.append(point)
    return extra


def _brightness_walk(parameters: MeasurementParameters, *, hs: bool) -> list[int]:
    if hs:
        return _brightness_values(
            parameters,
            parameters.min_brightness,
            parameters.max_brightness,
            parameters.hs_bri_steps,
            bisection=parameters.hs_bri_bisection,
            all_values=parameters.hs_bri_all,
        )
    return _brightness_values(
        parameters,
        parameters.min_brightness,
        parameters.max_brightness,
        parameters.ct_bri_steps,
        bisection=parameters.ct_bri_bisection,
        all_values=parameters.ct_bri_all,
    )


def _next_in_range_poi_sat_batch(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    known: set[Variation],
) -> SmartBatch | None:
    if mode != LutMode.HS:
        return None
    for hue, reason in hs_interest_targets(measured, parameters):
        if not _hue_in_range(hue, parameters):
            continue
        sats = _unmeasured(_hs_saturation_sweep_at_max(hue, parameters), known)
        if sats:
            return SmartBatch(sats, STAGE_DISCOVERY, f"{REASON_POI_SAT}: {reason}.")
    return None


def _hs_saturation_sweep_at_max(hue: int, parameters: MeasurementParameters) -> list[Variation]:
    """``hs_sat_divisions`` samples at 100% brightness on one hue — one pillar on the hue plot."""

    return [HsVariation(bri=parameters.max_brightness, hue=hue, sat=sat) for sat in sat_sweep_values(parameters)]


def _poi_sat_estimate(mode: LutMode, parameters: MeasurementParameters) -> int:
    """Saturation interiors still owed to midpoints after the primary-only outline."""

    if mode != LutMode.HS:
        return 0
    sats = [sat for sat in sat_sweep_values(parameters) if sat != parameters.max_sat]
    primaries = set(_hs_primary_hues(parameters))
    midpoints = [
        hue
        for hue in _hs_primary_and_midpoint_hues(parameters)
        if hue not in primaries and _hue_in_range(hue, parameters)
    ]
    return len(sats) * len(midpoints)


def _forced_ramp_estimate(mode: LutMode, parameters: MeasurementParameters) -> int:
    """Points a forced brightness walk will add beyond the 100% samples already in Phase A."""

    if mode == LutMode.COLOR_TEMP:
        extra = max(0, len(_brightness_walk(parameters, hs=False)) - 1)
        return extra * _FORCED_CT_RAMP_GUESS
    if mode == LutMode.HS:
        extra = max(0, len(_brightness_walk(parameters, hs=True)) - 1)
        return len(_hs_forced_colors(parameters)) * extra
    return 0


def _forced_targets(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[_ForcedTarget]:
    if mode == LutMode.COLOR_TEMP:
        return _ct_forced_targets(measured, parameters)
    if mode == LutMode.HS:
        return [
            target
            for target in _hs_forced_targets(parameters, measured)
            if isinstance(target.color, HsVariation) and _hue_in_range(target.color.hue, parameters)
        ]
    return []


def _forced_brightness_ramps(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[Variation]:
    """Full 0-255 brightness walks at the colors the 100% outline says are interesting."""

    walk = _brightness_walk(parameters, hs=mode == LutMode.HS)
    return [
        with_brightness(target.color, bri) for target in _forced_targets(mode, measured, parameters) for bri in walk
    ]


def _hs_forced_colors(
    parameters: MeasurementParameters,
    measured: Sequence[MeasuredPoint] = (),
) -> list[Variation]:
    """RGB primaries and midpoints at both sat ends, plus distant full-sat local maxima."""

    return [
        target.color
        for target in _hs_forced_targets(parameters, measured)
        if isinstance(target.color, HsVariation) and _hue_in_range(target.color.hue, parameters)
    ]


def _hs_forced_targets(
    parameters: MeasurementParameters,
    measured: Sequence[MeasuredPoint] = (),
) -> list[_ForcedTarget]:
    primaries = _hs_primary_hues(parameters)
    midpoints = [hue for hue in _hs_primary_and_midpoint_hues(parameters) if hue not in set(primaries)]
    targets: list[_ForcedTarget] = []
    already = list(primaries)
    for hue in primaries:
        for sat in (parameters.max_sat, parameters.min_sat):
            targets.append(
                _ForcedTarget(
                    HsVariation(bri=parameters.max_brightness, hue=hue, sat=sat),
                    f"{_primary_name(hue, primaries)} at {_hue_degrees(hue)}, {_sat_phrase(sat, parameters)}",
                )
            )
    for hue in midpoints:
        already.append(hue)
        for sat in (parameters.max_sat, parameters.min_sat):
            targets.append(
                _ForcedTarget(
                    HsVariation(bri=parameters.max_brightness, hue=hue, sat=sat),
                    f"primary midpoint at {_hue_degrees(hue)}, {_sat_phrase(sat, parameters)}",
                )
            )
    for hue in _hs_full_sat_local_max_hues(measured, parameters):
        if _hue_too_close(hue, already, parameters):
            continue
        targets.append(
            _ForcedTarget(
                HsVariation(bri=parameters.max_brightness, hue=hue, sat=parameters.max_sat),
                f"local maximum at {_hue_degrees(hue)}, full saturation",
            )
        )
        already.append(hue)
    return targets


_PRIMARY_NAMES = ("red primary", "green primary", "blue primary")


def _primary_name(hue: int, primaries: Sequence[int]) -> str:
    try:
        return _PRIMARY_NAMES[list(primaries).index(hue)]
    except (ValueError, IndexError):
        return "RGB primary"


def _hue_degrees(hue: int) -> str:
    return f"{round(hue / 65535 * 360)}°"


def _sat_phrase(sat: int, parameters: MeasurementParameters) -> str:
    if sat == parameters.max_sat:
        return "full saturation"
    if sat == parameters.min_sat:
        return "white"
    return f"{round(sat / 255 * 100)}% saturation"


def _kelvin_label(ct: int) -> str:
    return f"{round(1_000_000 / ct)} K" if ct > 0 else f"{ct} mired"


def _hs_primary_and_midpoint_hues(parameters: MeasurementParameters) -> list[int]:
    primaries = _hs_primary_hues(parameters)
    if len(primaries) < 2:
        return primaries
    midpoints: list[int] = []
    for left, right in zip(primaries, primaries[1:] + primaries[:1], strict=True):
        forward = (right - left) % HUE_MODULO
        midpoints.append(_wrap_hue(left + forward // 2))
    return list(dict.fromkeys([*primaries, *midpoints]))


def _hs_full_sat_level(measured: Sequence[MeasuredPoint], parameters: MeasurementParameters) -> int:
    """Highest sat actually present at 100% brightness — the real full-sat rail.

    A refine that lowered ``max_sat`` still has the seed's sat=255 ring; using
    the request cap would rediscover local maxima on a near-white slice.
    """
    sats = [
        point.variation.sat
        for point in measured
        if point.variation.bri == parameters.max_brightness and isinstance(point.variation, HsVariation)
    ]
    return max(sats) if sats else parameters.max_sat


def _hs_full_sat_ring(measured: Sequence[MeasuredPoint], parameters: MeasurementParameters) -> list[tuple[int, float]]:
    sat_max = _hs_full_sat_level(measured, parameters)
    by_hue: dict[int, float] = {}
    for point in measured:
        variation = point.variation
        if (
            variation.bri == parameters.max_brightness
            and isinstance(variation, HsVariation)
            and variation.sat == sat_max
        ):
            by_hue[variation.hue] = max(by_hue.get(variation.hue, point.watt), point.watt)
    return sorted(by_hue.items())


def _hs_full_sat_local_max_hues(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[int]:
    """Full-sat 100% peaks, hottest first, so a later closer peak can be skipped."""

    ring = _hs_full_sat_ring(measured, parameters)
    if len(ring) < 3:
        return []
    watts = [watt for _, watt in ring]
    p_peak = max(watts)
    threshold = _extremum_prominence_threshold(parameters)
    peaks: list[tuple[float, int]] = []
    for index, (hue, watt) in enumerate(ring):
        if _isolated_spike(watts, index, p_peak):
            continue
        if not _circular_local_max(watts, index):
            continue
        if plot_y(_circular_prominence(watts, index), p_peak) < threshold:
            continue
        peaks.append((watt, hue))
    peaks.sort(reverse=True)
    return [hue for _, hue in peaks]


def _circular_local_max(watts: Sequence[float], index: int) -> bool:
    count = len(watts)
    left, mid, right = watts[(index - 1) % count], watts[index], watts[(index + 1) % count]
    return (mid > left and mid >= right) or (mid >= left and mid > right)


def _circular_prominence(watts: Sequence[float], index: int) -> float:
    """1-D topographic prominence of a local max on a ring."""

    count = len(watts)
    height = watts[index]

    def saddle(step: int) -> float:
        lowest = height
        cursor = index
        for _ in range(count - 1):
            cursor = (cursor + step) % count
            lowest = min(lowest, watts[cursor])
            if watts[cursor] > height:
                return lowest
        return lowest

    return height - max(saddle(1), saddle(-1))


def _hue_distance(left: int, right: int) -> int:
    forward = (right - left) % HUE_MODULO
    return min(forward, HUE_MODULO - forward)


def _hue_too_close(hue: int, already: Sequence[int], parameters: MeasurementParameters) -> bool:
    """True when ``hue`` sits within border-Δ of an already-forced hue on the wheel."""

    threshold = max(1, round(HUE_MODULO * smart_border_delta(parameters) / 100.0))
    return any(_hue_distance(hue, other) <= threshold for other in already)


def ct_interest_targets(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[tuple[int, str]]:
    """Mireds the 100% outline marked for a forced brightness walk, with why."""

    return [
        (target.color.ct, target.reason)
        for target in _ct_forced_targets(measured, parameters)
        if isinstance(target.color, ColorTempVariation)
    ]


def hs_interest_targets(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[tuple[int, str]]:
    """Hues that get a 100% saturation sweep (and later a brightness walk), with why.

    Primaries and midpoints are the fixture's RGB angles on the full wheel,
    not the current run's hue slider. Local maxima come from the measured
    full-sat ring (the highest sat present at 100% brightness, so a refine
    that lowered max_sat still sees the seed's sat=255 rail). One reason per
    hue: full saturation wins over white when both ends are forced.
    """

    reasons: dict[int, str] = {}
    for target in _hs_forced_targets(parameters, measured):
        if isinstance(target.color, HsVariation):
            reasons.setdefault(target.color.hue, target.reason)
    return list(reasons.items())


def _ct_forced_colors(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[Variation]:
    """CTs that get a full brightness walk: discontinuities, per-segment max/min, extrema."""

    return [target.color for target in _ct_forced_targets(measured, parameters)]


def _ct_forced_targets(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[_ForcedTarget]:
    at_max = _ct_at_max(measured, parameters.max_brightness)
    if len(at_max) < 3:
        return []
    cts = [ct for ct, _ in at_max]
    watts = [watt for _, watt in at_max]
    p_peak = max(watts)
    jump_left = _ct_jump_left_indices(at_max, watts, p_peak, parameters)
    selected: list[tuple[int, str]] = []
    selected.extend(_ct_segment_extrema(cts, watts, p_peak, jump_left))
    selected.extend(_ct_extremum_cts(cts, watts, p_peak, parameters))
    for left in jump_left:
        selected.append((cts[left], f"discontinuity at {_kelvin_label(cts[left])}"))
        selected.append((cts[left + 1], f"discontinuity at {_kelvin_label(cts[left + 1])}"))
    reasons: dict[int, str] = {}
    for ct, reason in selected:
        reasons.setdefault(ct, reason)
    cliff_cts = {cts[left] for left in jump_left} | {cts[left + 1] for left in jump_left}
    return [
        _ForcedTarget(ColorTempVariation(bri=parameters.max_brightness, ct=ct), reasons[ct])
        for ct in _dedupe_forced_cts(
            cts,
            watts,
            p_peak,
            [ct for ct, _ in selected],
            protected=cliff_cts,
        )
        if ct in reasons
    ]


def _ct_jump_left_indices(
    at_max: Sequence[tuple[int, float]],
    watts: Sequence[float],
    p_peak: float,
    parameters: MeasurementParameters,
) -> list[int]:
    ct_span = max(1, at_max[-1][0] - at_max[0][0])
    delta = smart_border_delta(parameters)
    jumps: list[int] = []
    for index, ((left_ct, left_watt), (right_ct, right_watt)) in enumerate(pairwise(at_max)):
        if _isolated_spike(watts, index, p_peak) or _isolated_spike(watts, index + 1, p_peak):
            continue
        if ct_watt_jump(
            left_ct,
            left_watt,
            right_ct,
            right_watt,
            ct_span=ct_span,
            p_peak=p_peak,
            delta=delta,
        ):
            jumps.append(index)
    return jumps


def _ct_segments(count: int, jump_left: Sequence[int]) -> list[tuple[int, int]]:
    """Inclusive index ranges of contiguous 100% samples between hardware cliffs."""

    segments: list[tuple[int, int]] = []
    start = 0
    for left in jump_left:
        if start <= left:
            segments.append((start, left))
        start = left + 1
    if start <= count - 1:
        segments.append((start, count - 1))
    return segments


def _ct_segment_extrema(
    cts: Sequence[int],
    watts: Sequence[float],
    p_peak: float,
    jump_left: Sequence[int],
) -> list[tuple[int, str]]:
    """Global max and min of each isolated CT segment, ignoring lone outlier needles."""

    picked: list[tuple[int, str]] = []
    for start, end in _ct_segments(len(cts), jump_left):
        usable = [index for index in range(start, end + 1) if not _isolated_spike(watts, index, p_peak)]
        if not usable:
            continue
        max_ct = cts[max(usable, key=lambda index: watts[index])]
        min_ct = cts[min(usable, key=lambda index: watts[index])]
        picked.append((max_ct, f"segment maximum at {_kelvin_label(max_ct)}"))
        picked.append((min_ct, f"segment minimum at {_kelvin_label(min_ct)}"))
    return picked


def _ct_extremum_cts(
    cts: Sequence[int],
    watts: Sequence[float],
    p_peak: float,
    parameters: MeasurementParameters,
) -> list[tuple[int, str]]:
    threshold = _extremum_prominence_threshold(parameters)
    picked: list[tuple[int, str]] = []
    for index in range(1, len(watts) - 1):
        if _isolated_spike(watts, index, p_peak):
            continue
        if _isolated_spike(watts, index - 1, p_peak) or _isolated_spike(watts, index + 1, p_peak):
            continue
        kind = _local_extremum(watts, index)
        if kind is None:
            continue
        prominence = plot_y(_extremum_prominence(watts, index, peak=kind == "max"), p_peak)
        if prominence >= threshold:
            label = "local maximum" if kind == "max" else "local minimum"
            picked.append((cts[index], f"{label} at {_kelvin_label(cts[index])}"))
    return picked


def _dedupe_forced_cts(
    cts: Sequence[int],
    watts: Sequence[float],
    p_peak: float,
    selected: Sequence[int],
    protected: Collection[int] = (),
) -> list[int]:
    """Drop CTs that sit on the same peak. Keep both sides of a hardware cliff.

    Adjacent mireds with a real watt jump are not the same feature. Merging them
    walks only one side of the WW/CW discontinuity.
    """

    keep = set(protected)
    spike_cts = {cts[index] for index in range(len(watts)) if _isolated_spike(watts, index, p_peak)}
    unique: list[int] = []
    for ct in dict.fromkeys(selected):
        if ct in spike_cts:
            continue
        if ct not in keep and any(abs(ct - other) <= 2 and other not in keep for other in unique):
            continue
        unique.append(ct)
    return unique


def _extremum_prominence_threshold(parameters: MeasurementParameters) -> float:
    """Plot-y units. Default border Δ=8 → 2.0, so ~2% of peak (~0.1 W on a 5 W lamp)."""

    return max(2.0, smart_border_delta(parameters) / 4.0)


def _local_extremum(watts: Sequence[float], index: int) -> str | None:
    left, mid, right = watts[index - 1], watts[index], watts[index + 1]
    if (mid > left and mid >= right) or (mid >= left and mid > right):
        return "max"
    if (mid < left and mid <= right) or (mid <= left and mid < right):
        return "min"
    return None


def _extremum_prominence(watts: Sequence[float], index: int, *, peak: bool) -> float:
    """1-D topographic prominence of a local max, or of a local min when ``peak`` is false."""

    series = list(watts) if peak else [-watt for watt in watts]
    height = series[index]
    left = height
    for cursor in range(index - 1, -1, -1):
        left = min(left, series[cursor])
        if series[cursor] > height:
            break
    right = height
    for cursor in range(index + 1, len(series)):
        right = min(right, series[cursor])
        if series[cursor] > height:
            break
    return height - max(left, right)


def _isolated_spike(watts: Sequence[float], index: int, p_peak: float) -> bool:
    """True when this sample is a lone needle both neighbors agree is not the envelope."""

    if index <= 0 or index >= len(watts) - 1:
        return False
    left, mid, right = watts[index - 1], watts[index], watts[index + 1]
    neighbor_gap = abs(plot_y(left, p_peak) - plot_y(right, p_peak))
    drop_left = abs(plot_y(mid, p_peak) - plot_y(left, p_peak))
    drop_right = abs(plot_y(mid, p_peak) - plot_y(right, p_peak))
    return (
        neighbor_gap < _ISOLATED_NEIGHBOR_PLOT_Y
        and drop_left > _ISOLATED_SPIKE_PLOT_Y
        and drop_right > _ISOLATED_SPIKE_PLOT_Y
    )


def _perimeter_trace(
    envelope_colors: Sequence[Variation],
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[Variation]:
    p_peak = max(point.watt for point in measured)
    known = {point.variation for point in measured}
    min_bri = parameters.min_brightness
    max_bri = parameters.max_brightness
    shaped: list[Variation] = []
    fresh: list[Variation] = []
    for color in envelope_colors:
        if _rail_has_shape(_rail_for(measured, color_key(color)), min_bri, max_bri):
            shaped.append(color)
        else:
            fresh.append(color)
    colors = list(shaped)
    if fresh:
        colors.append(max(fresh, key=lambda color: _watt_at_max(color, measured, max_bri) or 0.0))
    candidates = (
        with_brightness(color, bri)
        for color in colors
        for bri in _equidistant_arc_brightness(
            color,
            measured,
            min_bri,
            max_bri,
            p_peak,
            smart_border_delta(parameters),
        )
    )
    return _pack_plot_points(
        candidates,
        measured,
        parameters,
        known,
        smart_border_delta(parameters),
    )


def _coverage_variations(
    envelope_colors: Sequence[Variation],
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    known: set[Variation],
) -> list[Variation]:
    p_peak = max(point.watt for point in measured)
    delta = smart_delta(parameters)
    delta_watt = (delta / 100.0) * max(p_peak, 1e-6)
    rails = _coverage_rails(measured, envelope_colors, parameters.max_brightness, delta_watt)
    min_bri = parameters.min_brightness
    max_bri = parameters.max_brightness
    span = max(1, max_bri - min_bri)
    step = max(1, round(span * (delta / 2.0) / 100.0))
    bris = list(range(min_bri, max_bri + 1, step))
    if max_bri not in bris:
        bris.append(max_bri)
    extra: list[Variation] = []
    seen: set[Variation] = set()
    for color in rails:
        key = color_key(color)
        cover = [point for point in measured if color_key(point.variation) == key]
        batch = _pack_plot_points(
            (with_brightness(color, bri) for bri in bris),
            measured,
            parameters,
            known | seen,
            delta,
            cover_with=cover,
        )
        extra.extend(batch)
        seen.update(batch)
    return extra


_DART_BATCH = 12
_DART_ATTEMPTS = 96
_DART_BRI_EXP = 0.45
_DART_SHRINK = 0.7


def _dart_coverage(
    mode: LutMode,
    envelope_colors: Sequence[Variation],
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
    known: set[Variation],
) -> list[Variation]:
    """Random interior samples, biased toward high power, skipping guesses closer than the radius.

    Starts at interior Δ. A throw round that accepts nothing shrinks the radius by 0.7
    and tries again in the same call, so resume replay stays a function of measured
    points only. Stops when the floor accepts nothing.
    """

    radius = smart_delta(parameters)
    floor = min(smart_dart_floor(parameters), radius)
    rng = _dart_rng(measured, parameters)
    while True:
        thrown = _throw_dart_batch(
            mode,
            envelope_colors,
            measured,
            parameters,
            light_info,
            known,
            radius,
            rng,
        )
        if thrown:
            return thrown
        if radius <= floor + 1e-9:
            return []
        radius = max(floor, radius * _DART_SHRINK)


def _dart_rng(measured: Sequence[MeasuredPoint], parameters: MeasurementParameters) -> random.Random:
    digest = hashlib.sha256()
    digest.update(
        f"{parameters.smart_delta:.6f}:{parameters.smart_dart_min_delta:.6f}:"
        f"{parameters.min_brightness}:{parameters.max_brightness}:"
        f"{len(measured)}".encode()
    )
    for point in sorted(measured, key=_dart_sort_key):
        digest.update(repr(color_key(point.variation)).encode())
        digest.update(struct.pack(">id", point.variation.bri, float(round(point.watt, 6))))
    return random.Random(int.from_bytes(digest.digest()[:8], "big"))


def _dart_sort_key(point: MeasuredPoint) -> tuple[object, ...]:
    return (point.variation.bri, color_key(point.variation), round(point.watt, 6))


def _throw_dart_batch(
    mode: LutMode,
    envelope_colors: Sequence[Variation],
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
    known: set[Variation],
    radius: float,
    rng: random.Random,
) -> list[Variation]:
    min_bri = parameters.min_brightness
    max_bri = parameters.max_brightness
    if max_bri - min_bri < 2:
        return []
    span = max_bri - min_bri
    p_peak = max(point.watt for point in measured)
    placed = [(plot_x(point.variation.bri, min_bri, max_bri), plot_y(point.watt, p_peak)) for point in measured]
    at_max = [(point.variation, point.watt) for point in measured if point.variation.bri == max_bri]
    extra: list[Variation] = []
    seen = set(known)
    for _ in range(_DART_ATTEMPTS):
        if len(extra) >= _DART_BATCH:
            break
        color = _pick_dart_color(mode, at_max, envelope_colors, parameters, light_info, rng)
        u = rng.random()
        bri = int(round(min_bri + (u**_DART_BRI_EXP) * span))
        bri = min(max_bri - 1, max(min_bri + 1, bri))
        variation = with_brightness(color, bri)
        if variation in seen:
            continue
        watt = estimated_watt(variation, measured, min_bri, max_bri)
        x = plot_x(variation.bri, min_bri, max_bri)
        y = plot_y(watt, p_peak)
        if any(math.hypot(x - px, y - py) < radius for px, py in placed):
            continue
        seen.add(variation)
        extra.append(variation)
        placed.append((x, y))
    return extra


def _pick_dart_color(
    mode: LutMode,
    at_max: Sequence[tuple[Variation, float]],
    envelope_colors: Sequence[Variation],
    parameters: MeasurementParameters,
    light_info: LightInfo,
    rng: random.Random,
) -> Variation:
    weighted = list(at_max)
    for color in envelope_colors:
        if all(color_key(color) != color_key(existing) for existing, _watt in weighted):
            weighted.append((color, 1.0))
    if weighted and rng.random() < 0.55:
        colors, watts = zip(*weighted, strict=True)
        return rng.choices(list(colors), weights=[max(watt, 1e-3) for watt in watts], k=1)[0]
    if mode == LutMode.COLOR_TEMP:
        cool, warm = clamp_mired_range(light_info, parameters.min_kelvin, parameters.max_kelvin)
        return ColorTempVariation(bri=parameters.max_brightness, ct=rng.randint(cool, warm))
    hues = hue_sweep_values(parameters)
    sats = sat_sweep_values(parameters)
    hue = rng.choice(hues) if hues and rng.random() < 0.5 else rng.randint(parameters.min_hue, parameters.max_hue)
    sat = rng.choice(sats) if sats and rng.random() < 0.5 else rng.randint(parameters.min_sat, parameters.max_sat)
    return HsVariation(bri=parameters.max_brightness, hue=hue, sat=sat)


def plot_x(bri: int, min_bri: int, max_bri: int) -> float:
    span = max_bri - min_bri
    if span <= 0:
        return 0.0
    return 100.0 * (bri - min_bri) / span


def plot_y(watt: float, p_peak: float) -> float:
    return 100.0 * watt / max(p_peak, 1e-6)


def plot_distance(
    bri_a: int,
    watt_a: float,
    bri_b: int,
    watt_b: float,
    min_bri: int,
    max_bri: int,
    p_peak: float,
) -> float:
    dx = plot_x(bri_a, min_bri, max_bri) - plot_x(bri_b, min_bri, max_bri)
    dy = plot_y(watt_a, p_peak) - plot_y(watt_b, p_peak)
    return math.hypot(dx, dy)


def interpolate_watt(rail: Sequence[tuple[int, float]], bri: int) -> float:
    if not rail:
        return 0.0
    ordered = sorted(rail)
    if bri <= ordered[0][0]:
        return ordered[0][1]
    if bri >= ordered[-1][0]:
        return ordered[-1][1]
    for (left_bri, left_watt), (right_bri, right_watt) in pairwise(ordered):
        if left_bri <= bri <= right_bri:
            if left_bri == right_bri:
                return left_watt
            t = (bri - left_bri) / (right_bri - left_bri)
            return left_watt + t * (right_watt - left_watt)
    return ordered[-1][1]


def envelope_watts(
    measured: Sequence[MeasuredPoint],
    bri: int,
    min_bri: int,
    max_bri: int,
) -> tuple[float, float] | None:
    """P_min(b), P_max(b) from the current hottest/coldest rails, or None."""

    max_bri_points = [point for point in measured if point.variation.bri == max_bri]
    if not max_bri_points:
        return None
    hot = max(max_bri_points, key=lambda point: point.watt)
    cold = min(max_bri_points, key=lambda point: point.watt)
    hot_rail = _rail_for(measured, color_key(hot.variation))
    cold_rail = _rail_for(measured, color_key(cold.variation))
    if not hot_rail or not cold_rail:
        return None
    p_max = interpolate_watt(hot_rail, bri)
    p_min = interpolate_watt(cold_rail, bri)
    if p_min > p_max:
        p_min, p_max = p_max, p_min
    return p_min, p_max


def _color_distance(left: Variation, right: Variation) -> float:
    if isinstance(left, ColorTempVariation) and isinstance(right, ColorTempVariation):
        return float(abs(left.ct - right.ct))
    if isinstance(left, HsVariation) and isinstance(right, HsVariation):
        hue = min((left.hue - right.hue) % HUE_MODULO, (right.hue - left.hue) % HUE_MODULO)
        return math.hypot(hue / HUE_MODULO * 100.0, (left.sat - right.sat) / 2.55)
    return math.inf


def _watt_at_max(variation: Variation, measured: Sequence[MeasuredPoint], max_bri: int) -> float | None:
    for bri, watt in _rail_for(measured, color_key(variation)):
        if bri == max_bri:
            return watt
    at_max = [point for point in measured if point.variation.bri == max_bri]
    if not at_max:
        return None
    nearest = min(at_max, key=lambda point: _color_distance(variation, point.variation))
    if math.isinf(_color_distance(variation, nearest.variation)):
        return None
    return nearest.watt


def _rail_scale(
    measured: Sequence[MeasuredPoint],
    key: tuple[object, ...],
    bri: int,
    max_bri: int,
) -> float | None:
    rail = _rail_for(measured, key)
    if len(rail) < 2:
        return None
    peak = interpolate_watt(rail, max_bri)
    if peak <= 1e-9:
        return None
    return interpolate_watt(rail, bri) / peak


def _rail_has_shape(rail: Sequence[tuple[int, float]], min_bri: int, max_bri: int) -> bool:
    """True once this color has an interior sample, not only the two brightness ends."""

    return any(min_bri < bri < max_bri for bri, _ in rail)


def _bracket_gap(rail: Sequence[tuple[int, float]], bri: int) -> int | None:
    """Brightness span of the measured pair that contains ``bri``, or None if extrapolating."""

    ordered = sorted(rail)
    if len(ordered) < 2:
        return None
    for (left_bri, _), (right_bri, _) in pairwise(ordered):
        if left_bri <= bri <= right_bri:
            return right_bri - left_bri
    return None


def _same_bri_scaled_watt(
    variation: Variation,
    measured: Sequence[MeasuredPoint],
    max_bri: int,
) -> float | None:
    """Guess from a sample already taken at this brightness, scaled by the 100% sweep."""

    peak = _watt_at_max(variation, measured, max_bri)
    if peak is None:
        return None
    same_bri = [
        point
        for point in measured
        if point.variation.bri == variation.bri
        and color_key(point.variation) != color_key(variation)
        and math.isfinite(_color_distance(variation, point.variation))
    ]
    if not same_bri:
        return None
    nearest = min(same_bri, key=lambda point: _color_distance(variation, point.variation))
    neighbor_peak = _watt_at_max(nearest.variation, measured, max_bri)
    if neighbor_peak is None or neighbor_peak <= 1e-9:
        return None
    return peak * (nearest.watt / neighbor_peak)


def _neighbor_scaled_watt(
    variation: Variation,
    measured: Sequence[MeasuredPoint],
    min_bri: int,
    max_bri: int,
) -> float | None:
    """100% watt for this color, scaled by the nearest rails that already have a curve."""

    peak = _watt_at_max(variation, measured, max_bri)
    if peak is None:
        return None
    scales: list[tuple[float, float]] = []
    seen_keys: set[tuple[object, ...]] = set()
    for point in measured:
        key = color_key(point.variation)
        if key in seen_keys or key == color_key(variation):
            continue
        seen_keys.add(key)
        rail = _rail_for(measured, key)
        if _bracket_gap(rail, variation.bri) is None:
            continue
        scale = _rail_scale(measured, key, variation.bri, max_bri)
        if scale is None:
            continue
        distance = _color_distance(variation, with_brightness(point.variation, variation.bri))
        if math.isfinite(distance):
            scales.append((distance, scale))
    if not scales:
        return None
    scales.sort()
    nearest = scales[:2]
    if nearest[0][0] <= 1e-9:
        return peak * nearest[0][1]
    weight_sum = 0.0
    scale_sum = 0.0
    for distance, scale in nearest:
        weight = 1.0 / max(distance, 1e-9)
        weight_sum += weight
        scale_sum += weight * scale
    return peak * (scale_sum / weight_sum)


def estimated_watt(
    variation: Variation,
    measured: Sequence[MeasuredPoint],
    min_bri: int,
    max_bri: int,
) -> float:
    """Guess watt from this color's own rail, else 100% sweep × neighbor brightness curves."""

    rail = _rail_for(measured, color_key(variation))
    measured_here = next((watt for bri, watt in rail if bri == variation.bri), None)
    if measured_here is not None:
        return measured_here
    gap = _bracket_gap(rail, variation.bri)
    if gap is not None and gap <= 40:
        return interpolate_watt(rail, variation.bri)
    same_bri_guess = _same_bri_scaled_watt(variation, measured, max_bri)
    if same_bri_guess is not None:
        return same_bri_guess
    neighbor_guess = _neighbor_scaled_watt(variation, measured, min_bri, max_bri)
    if neighbor_guess is not None:
        return neighbor_guess
    if len(rail) >= 2:
        return interpolate_watt(rail, variation.bri)

    peak = _watt_at_max(variation, measured, max_bri)
    band = envelope_watts(measured, variation.bri, min_bri, max_bri)
    if peak is not None and band is not None:
        band_max = envelope_watts(measured, max_bri, min_bri, max_bri)
        if band_max is not None:
            mid_here = (band[0] + band[1]) / 2
            mid_max = (band_max[0] + band_max[1]) / 2
            if mid_max > 1e-9:
                return peak * (mid_here / mid_max)
    if band is not None:
        return (band[0] + band[1]) / 2
    if peak is not None:
        return peak
    return 0.0


def _pack_plot_points(
    candidates: Iterable[Variation],
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    known: set[Variation],
    delta: float,
    *,
    cover_with: Sequence[MeasuredPoint] | None = None,
) -> list[Variation]:
    """Keep candidates whose guessed (bri, watt) is at least Δ from every accepted point.

    ``cover_with`` defaults to all measured points (dart fill). Coverage passes one
    color's samples so a dense rail cannot retire a different color's holes.
    """

    min_bri = parameters.min_brightness
    max_bri = parameters.max_brightness
    p_peak = max(point.watt for point in measured)
    neighbors = measured if cover_with is None else cover_with
    placed = [(plot_x(point.variation.bri, min_bri, max_bri), plot_y(point.watt, p_peak)) for point in neighbors]
    scored: list[tuple[float, Variation, float, float]] = []
    seen = set(known)
    for variation in candidates:
        if variation in seen:
            continue
        seen.add(variation)
        watt = estimated_watt(variation, measured, min_bri, max_bri)
        x = plot_x(variation.bri, min_bri, max_bri)
        y = plot_y(watt, p_peak)
        nearest = min((math.hypot(x - px, y - py) for px, py in placed), default=math.inf)
        scored.append((nearest, variation, x, y))
    scored.sort(key=lambda item: item[0], reverse=True)
    extra: list[Variation] = []
    for _distance, variation, x, y in scored:
        if any(math.hypot(x - px, y - py) < delta for px, py in placed):
            continue
        extra.append(variation)
        placed.append((x, y))
    return extra


def _hs_outline(parameters: MeasurementParameters) -> list[Variation]:
    """100% brightness HS outline: hue ring, white, then sat samples on RGB primaries."""

    max_bri = parameters.max_brightness
    hues = hue_sweep_values(parameters)
    ordered: list[Variation] = []
    seen: set[Variation] = set()

    def add(point: Variation) -> None:
        if point not in seen:
            seen.add(point)
            ordered.append(point)

    for hue in hues:
        add(HsVariation(bri=max_bri, hue=hue, sat=parameters.max_sat))
    white_hue = hues[0] if hues else _clamp_hue(HUE_MIN, parameters)
    add(HsVariation(bri=max_bri, hue=white_hue, sat=parameters.min_sat))
    skip = {parameters.min_sat, parameters.max_sat}
    for hue in _hs_primary_hues(parameters):
        if not _hue_in_range(hue, parameters):
            continue
        for sat in sat_sweep_values(parameters):
            if sat in skip:
                continue
            add(HsVariation(bri=max_bri, hue=hue, sat=sat))
    return ordered


def _hs_primary_hues(parameters: MeasurementParameters) -> list[int]:
    """RGB primaries on the hue wheel — the fixture's emitters, not the slider.

    Clamping these into a narrowed ``min_hue``/``max_hue`` invented a fake
    primary at the edge and moved every midpoint tick on the hue-at-100% plot.
    ``parameters`` is kept so call sites stay unchanged; the angles do not
    depend on the current run's hue range.
    """
    del parameters
    return [HUE_MIN, HUE_MODULO // 3, (2 * HUE_MODULO) // 3]


def _scout_colors(mode: LutMode, parameters: MeasurementParameters, light_info: LightInfo) -> list[Variation]:
    if mode == LutMode.COLOR_TEMP:
        cool, warm = clamp_mired_range(light_info, parameters.min_kelvin, parameters.max_kelvin)
        return [ColorTempVariation(bri=parameters.max_brightness, ct=mired) for mired in (cool, warm)]
    return [
        HsVariation(bri=parameters.max_brightness, hue=hue, sat=parameters.max_sat)
        for hue in _hs_primary_hues(parameters)
        if _hue_in_range(hue, parameters)
    ]


def _wrap_hue(hue: int) -> int:
    wrapped = hue % HUE_MODULO
    return HUE_MIN if wrapped == 0 else wrapped


def _hue_in_range(hue: int, parameters: MeasurementParameters) -> bool:
    return parameters.min_hue <= hue <= parameters.max_hue


def _clamp_hue(hue: int, parameters: MeasurementParameters) -> int:
    return min(max(_wrap_hue(hue), parameters.min_hue), parameters.max_hue)


def _post_scout_colors(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> list[Variation]:
    max_bri = parameters.max_brightness
    at_max = [point for point in measured if point.variation.bri == max_bri]
    if not at_max:
        return []
    if mode == LutMode.COLOR_TEMP:
        return _ct_extra_colors(at_max, parameters, light_info)
    return _hs_sat_mid_colors(at_max, parameters)


def power_mix_mired(
    cool: int,
    warm: int,
    p_cool: float,
    p_warm: float,
    rated: float | None,
) -> int | None:
    """Mired where the weaker white is faded in on top of the stronger one, up to ``rated``.

    One extra guess for one common WW/CW mix: keep the stronger white at 100%
    and fade in as much of the weaker as fits under ``rated``. It is not the
    measured peak and is not treated as one — the 100% sweep and the hottest /
    coldest samples still decide the envelope. Without a rating this is skipped.
    """

    if rated is None or rated <= 0:
        return None
    if p_cool >= p_warm:
        dominant_mired, dominant_watt, other_mired, other_watt = cool, p_cool, warm, p_warm
    else:
        dominant_mired, dominant_watt, other_mired, other_watt = warm, p_warm, cool, p_cool
    if other_watt <= 0:
        return None
    fraction = min(1.0, max(0.0, (rated - dominant_watt) / other_watt))
    return round(dominant_mired + fraction * (other_mired - dominant_mired))


def _ct_extra_colors(
    at_max: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> list[Variation]:
    cool, warm = clamp_mired_range(light_info, parameters.min_kelvin, parameters.max_kelvin)
    by_ct = {point.variation.ct: point.watt for point in at_max if isinstance(point.variation, ColorTempVariation)}
    if cool not in by_ct or warm not in by_ct:
        return []
    mix = power_mix_mired(cool, warm, by_ct[cool], by_ct[warm], parameters.rated_power)
    if mix is None or mix in by_ct:
        return []
    return [ColorTempVariation(bri=parameters.max_brightness, ct=mix)]


def _rail_colors(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> list[Variation]:
    if mode == LutMode.COLOR_TEMP:
        return _ct_rail_colors(measured, parameters, light_info)
    return _envelope_colors(measured, parameters.max_brightness, ())


def _ct_rail_colors(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
    light_info: LightInfo,
) -> list[Variation]:
    """Always-traced CT rails: ends, mired midpoint, optional mix guess, plus measured extrema."""

    cool, warm = clamp_mired_range(light_info, parameters.min_kelvin, parameters.max_kelvin)
    max_bri = parameters.max_brightness
    at_max = _ct_at_max(measured, max_bri)
    by_ct = dict(at_max)
    mireds = [cool, warm, (cool + warm) // 2]
    if cool in by_ct and warm in by_ct:
        mix = power_mix_mired(cool, warm, by_ct[cool], by_ct[warm], parameters.rated_power)
        if mix is not None:
            mireds.append(mix)
    if at_max:
        mireds.append(max(at_max, key=lambda item: item[1])[0])
        mireds.append(min(at_max, key=lambda item: item[1])[0])
    unique: list[Variation] = []
    seen: set[int] = set()
    for mired in mireds:
        if mired < cool or mired > warm:
            continue
        if any(abs(mired - other) <= 2 and not _ct_pair_is_cliff(mired, other, by_ct) for other in seen):
            continue
        seen.add(mired)
        unique.append(ColorTempVariation(bri=max_bri, ct=mired))
    return unique


def _ct_at_max(measured: Sequence[MeasuredPoint], max_bri: int) -> list[tuple[int, float]]:
    return sorted(
        (point.variation.ct, point.watt)
        for point in measured
        if point.variation.bri == max_bri and isinstance(point.variation, ColorTempVariation)
    )


def _evenly_spaced(start: int, end: int, intervals: int) -> list[int]:
    if intervals <= 0 or start == end:
        return [start]
    return list(dict.fromkeys(round(start + i * (end - start) / intervals) for i in range(intervals + 1)))


def _max_bri_watt_gaps(
    mode: LutMode,
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[Variation]:
    """Fill remaining plot-y gaps on the 100% color arc so the top edge is ~Δ apart."""

    if mode == LutMode.COLOR_TEMP:
        return _ct_max_bri_watt_gaps(measured, parameters)
    if mode == LutMode.HS:
        return _hs_max_bri_sat_watt_gaps(measured, parameters) + _hs_max_bri_hue_watt_gaps(measured, parameters)
    return []


def _ct_pair_is_cliff(
    left_ct: int,
    right_ct: int,
    by_ct: Mapping[int, float],
    *,
    delta: float = 4.0,
) -> bool:
    """True when two nearby mireds are opposite sides of a WW/CW watt jump."""

    if left_ct not in by_ct or right_ct not in by_ct:
        return False
    lo, hi = (left_ct, right_ct) if left_ct <= right_ct else (right_ct, left_ct)
    watts = list(by_ct.values())
    if not watts:
        return False
    return ct_watt_jump(
        lo,
        by_ct[lo],
        hi,
        by_ct[hi],
        ct_span=max(1, max(by_ct) - min(by_ct)),
        p_peak=max(watts),
        delta=delta,
    )


def ct_watt_jump(
    left_ct: int,
    left_watt: float,
    right_ct: int,
    right_watt: float,
    *,
    ct_span: int,
    p_peak: float,
    delta: float,
) -> bool:
    """True when two 100% CTs are a hardware jump, not a hole a midpoint could land in.

    Plot-y is watts. Plot-x here is mired along the 100% sweep. A gap that is taller
    than Δ and narrower than Δ is a vertical cliff — no colour temperature in
    between produces the missing watts.
    """

    dy = abs(plot_y(left_watt, p_peak) - plot_y(right_watt, p_peak))
    if dy <= delta:
        return False
    dct = right_ct - left_ct
    if dct <= 1:
        return True
    dx = 100.0 * dct / max(ct_span, 1)
    return dx < delta


def _ct_max_bri_watt_gaps(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[Variation]:
    at_max = _ct_at_max(measured, parameters.max_brightness)
    if len(at_max) < 2:
        return []
    p_peak = max(watt for _, watt in at_max)
    delta = smart_border_delta(parameters)
    max_bri = parameters.max_brightness
    ct_span = max(1, at_max[-1][0] - at_max[0][0])
    extra: list[Variation] = []
    for (left_ct, left_watt), (right_ct, right_watt) in pairwise(at_max):
        if ct_watt_jump(
            left_ct,
            left_watt,
            right_ct,
            right_watt,
            ct_span=ct_span,
            p_peak=p_peak,
            delta=delta,
        ):
            continue
        distance = abs(plot_y(left_watt, p_peak) - plot_y(right_watt, p_peak))
        if distance <= delta:
            continue
        intervals = max(2, round(distance / delta))
        extra.extend(
            ColorTempVariation(bri=max_bri, ct=mired) for mired in _evenly_spaced(left_ct, right_ct, intervals)[1:-1]
        )
    return extra


def _hs_max_bri_sat_watt_gaps(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[Variation]:
    """Invent saturations on already-scouted hues so 100% HS watt gaps get a curve."""

    max_bri = parameters.max_brightness
    at_max = [
        (point.variation.hue, point.variation.sat, point.watt)
        for point in measured
        if point.variation.bri == max_bri and isinstance(point.variation, HsVariation)
    ]
    if len(at_max) < 2:
        return []
    p_peak = max(watt for _, _, watt in at_max)
    delta = smart_border_delta(parameters)
    by_hue: dict[int, list[tuple[int, float]]] = {}
    for hue, sat, watt in at_max:
        by_hue.setdefault(hue, []).append((sat, watt))
    extra: list[Variation] = []
    for hue, pairs in by_hue.items():
        ordered = sorted(pairs)
        if len(ordered) < 2:
            continue
        for (left_sat, left_watt), (right_sat, right_watt) in pairwise(ordered):
            if right_sat - left_sat <= 1:
                continue
            distance = abs(plot_y(left_watt, p_peak) - plot_y(right_watt, p_peak))
            if distance <= delta:
                continue
            intervals = max(2, round(distance / delta))
            extra.extend(
                HsVariation(bri=max_bri, hue=hue, sat=sat)
                for sat in _evenly_spaced(left_sat, right_sat, intervals)[1:-1]
            )
    return extra


def _hs_max_bri_hue_watt_gaps(
    measured: Sequence[MeasuredPoint],
    parameters: MeasurementParameters,
) -> list[Variation]:
    """Fill remaining plot-y gaps on the 100% full-sat hue ring."""

    max_bri = parameters.max_brightness
    ring = [
        (point.variation.hue, point.watt)
        for point in measured
        if point.variation.bri == max_bri
        and isinstance(point.variation, HsVariation)
        and point.variation.sat == parameters.max_sat
    ]
    if len(ring) < 2:
        return []
    p_peak = max(watt for _, watt in ring)
    delta = smart_border_delta(parameters)
    ordered = sorted(ring)
    pairs = list(pairwise(ordered))
    pairs.append((ordered[-1], ordered[0]))
    extra: list[Variation] = []
    for (left_hue, left_watt), (right_hue, right_watt) in pairs:
        forward = (right_hue - left_hue) % HUE_MODULO
        if forward <= 1:
            continue
        dy = abs(plot_y(left_watt, p_peak) - plot_y(right_watt, p_peak))
        dx = 100.0 * forward / HUE_MODULO
        if dy <= delta or (dy > delta and dx < delta):
            continue
        intervals = max(2, round(dy / delta))
        extra.extend(
            HsVariation(
                bri=max_bri, hue=_wrap_hue_value(left_hue + round(i * forward / intervals)), sat=parameters.max_sat
            )
            for i in range(1, intervals)
        )
    return extra


def _wrap_hue_value(hue: int) -> int:
    wrapped = hue % HUE_MODULO
    return HUE_MAX if wrapped < HUE_MIN else wrapped


def _hs_sat_mid_colors(at_max: Sequence[MeasuredPoint], parameters: MeasurementParameters) -> list[Variation]:
    hs_points = [point for point in at_max if isinstance(point.variation, HsVariation)]
    if not hs_points:
        return []
    hot = max(hs_points, key=lambda point: point.watt).variation
    cold = min(hs_points, key=lambda point: point.watt).variation
    sat_mid = (parameters.min_sat + parameters.max_sat) // 2
    known = {(point.variation.hue, point.variation.sat) for point in hs_points}
    return [
        HsVariation(bri=parameters.max_brightness, hue=hue, sat=sat_mid)
        for hue in dict.fromkeys((hot.hue, cold.hue))
        if (hue, sat_mid) not in known
    ]


def _envelope_colors(
    measured: Sequence[MeasuredPoint],
    max_bri: int,
    extras: Sequence[Variation],
) -> list[Variation]:
    at_max = [point for point in measured if point.variation.bri == max_bri]
    if not at_max:
        return []
    hot = max(at_max, key=lambda point: point.watt).variation
    cold = min(at_max, key=lambda point: point.watt).variation
    colors = [hot, cold, *extras]
    unique: list[Variation] = []
    seen: set[tuple[object, ...]] = set()
    for color in colors:
        key = color_key(color)
        if key in seen:
            continue
        seen.add(key)
        unique.append(color)
    return unique


def _rail_for(measured: Sequence[MeasuredPoint], key: tuple[object, ...]) -> list[tuple[int, float]]:
    return [(point.variation.bri, point.watt) for point in measured if color_key(point.variation) == key]


def _equidistant_arc_brightness(
    color: Variation,
    measured: Sequence[MeasuredPoint],
    min_bri: int,
    max_bri: int,
    p_peak: float,
    delta: float,
) -> list[int]:
    """Place brightness knots at ~Δ arc length along this color's plot-space rail."""

    if min_bri == max_bri:
        have = {bri for bri, _watt in _rail_for(measured, color_key(color))}
        return [] if max_bri in have else [max_bri]
    # A seed/parent rail may already walk 1-255. Only densify inside this run's span.
    rail = sorted((bri, watt) for bri, watt in _rail_for(measured, color_key(color)) if min_bri <= bri <= max_bri)
    if not rail:
        return [max_bri, min_bri]
    have = {bri for bri, _ in rail}
    if min_bri not in have:
        return [min_bri]
    if max_bri not in have:
        return [max_bri]
    needed: list[int] = []
    for (left_bri, left_watt), (right_bri, right_watt) in pairwise(rail):
        if right_bri - left_bri <= 1:
            continue
        distance = plot_distance(left_bri, left_watt, right_bri, right_watt, min_bri, max_bri, p_peak)
        intervals = max(1, round(distance / max(delta, 1e-6)))
        if intervals <= 1:
            continue
        for bri in _evenly_spaced(left_bri, right_bri, intervals)[1:-1]:
            if bri not in have and bri not in (left_bri, right_bri):
                have.add(bri)
                needed.append(bri)
    return needed


def _coverage_rails(
    measured: Sequence[MeasuredPoint],
    envelope_colors: Sequence[Variation],
    max_bri: int,
    delta_watt: float,
) -> list[Variation]:
    at_max = [point for point in measured if point.variation.bri == max_bri]
    if not at_max:
        return list(envelope_colors)
    ranked = sorted(at_max, key=lambda point: point.watt)
    watts = [point.watt for point in ranked]
    low, high = watts[0], watts[-1]
    targets = [low]
    level = low + delta_watt
    while level < high - delta_watt / 2:
        targets.append(level)
        level += delta_watt
    targets.append(high)
    picked: list[Variation] = []
    used: set[tuple[object, ...]] = set()
    by_ct = {point.variation.ct: point.watt for point in at_max if isinstance(point.variation, ColorTempVariation)}

    def _too_similar(color: Variation) -> bool:
        if color_key(color) in used:
            return True
        return any(
            isinstance(color, ColorTempVariation)
            and isinstance(existing, ColorTempVariation)
            and abs(color.ct - existing.ct) <= 2
            and not _ct_pair_is_cliff(color.ct, existing.ct, by_ct)
            for existing in picked
        )

    for target in targets:
        unused = [point for point in ranked if not _too_similar(point.variation)]
        if not unused:
            continue
        candidate = min(unused, key=lambda point: abs(point.watt - target))
        used.add(color_key(candidate.variation))
        picked.append(candidate.variation)
    for color in envelope_colors:
        if _too_similar(color):
            continue
        used.add(color_key(color))
        picked.append(color)
    return picked


def load_measured_points(rows: Iterable[tuple[Variation, float]]) -> list[MeasuredPoint]:
    return [MeasuredPoint(variation=variation, watt=watt) for variation, watt in rows]
