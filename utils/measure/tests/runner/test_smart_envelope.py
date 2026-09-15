from collections import defaultdict
from itertools import pairwise
from pathlib import Path

from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightInfo
from measure.runner.light_plan import (
    ColorTempVariation,
    HsVariation,
    color_temp_mireds,
    hue_sweep_values,
    sat_sweep_values,
    variation_from_csv_row,
)
from measure.runner.smart_envelope import (
    REASON_COVERAGE,
    REASON_CT_SWEEP,
    REASON_DART,
    REASON_ENVELOPE_ENDS,
    REASON_FORCED_RAMPS,
    REASON_HS_OUTLINE,
    REASON_POI_SAT,
    REASON_PERIMETER,
    REASON_POST_SCOUT,
    REASON_WATT_GAPS,
    STAGE_COVERAGE,
    STAGE_DISCOVERY,
    MeasuredPoint,
    _brightness_walk,
    _coverage_variations,
    _ct_forced_colors,
    _ct_forced_targets,
    _ct_max_bri_watt_gaps,
    _forced_brightness_ramps,
    _forced_ramp_estimate,
    _poi_sat_estimate,
    _hs_forced_colors,
    _hs_forced_targets,
    _hs_primary_and_midpoint_hues,
    _hs_primary_hues,
    hs_interest_targets,
    ct_watt_jump,
    envelope_watts,
    estimate_smart_mode,
    estimate_smart_remaining,
    estimated_watt,
    interpolate_watt,
    next_smart_batch,
    next_smart_variations,
    phase_a_variations,
    plot_distance,
    plot_y,
    power_mix_mired,
    replay_smart_remaining,
    smart_border_delta,
    smart_delta,
    smart_planner_has_work,
    smart_uninvented_count,
    with_brightness,
)
from measure.tuning import MeasurementParameters

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "36871"


def _light_info() -> LightInfo:
    return LightInfo(model_id="36871", min_mired=153, max_mired=555)


def _parameters(**overrides: object) -> MeasurementParameters:
    values = {
        "smart_sampling": True,
        "smart_delta": 8.0,
        "smart_border_delta": 8.0,
        "min_brightness": 1,
        "max_brightness": 255,
        "min_kelvin": 1800,
        "max_kelvin": 6535,
        "min_sat": 1,
        "max_sat": 255,
        "min_hue": 1,
        "max_hue": 65535,
    }
    values.update(overrides)
    return MeasurementParameters(**values)  # type: ignore[arg-type]


def _load_csv(name: str, mode: LutMode) -> list[MeasuredPoint]:
    points: list[MeasuredPoint] = []
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        header = handle.readline()
        assert header
        for line in handle:
            row = line.strip().split(",")
            variation = variation_from_csv_row(row, mode)
            if variation is None:
                continue
            points.append(MeasuredPoint(variation=variation, watt=float(row[-1])))
    return points


def test_first_smart_batch_explains_the_hardcoded_outline() -> None:
    hs = next_smart_batch(LutMode.HS, [], _parameters(), _light_info())
    assert hs.stage == STAGE_DISCOVERY
    assert hs.reason == REASON_HS_OUTLINE
    assert hs.variations
    ct = next_smart_batch(LutMode.COLOR_TEMP, [], _parameters(), _light_info())
    assert ct.reason == REASON_CT_SWEEP
    assert ct.variations


def test_post_outline_batch_has_a_specific_reason() -> None:
    parameters = _parameters()
    light = _light_info()
    outline = next_smart_batch(LutMode.HS, [], parameters, light)
    measured = [MeasuredPoint(point, 5.0) for point in outline.variations]
    nxt = next_smart_batch(LutMode.HS, measured, parameters, light)
    assert (
        nxt.reason
        in {
            REASON_POST_SCOUT,
            REASON_WATT_GAPS,
            REASON_ENVELOPE_ENDS,
            REASON_PERIMETER,
            REASON_COVERAGE,
            REASON_DART,
        }
        or nxt.reason.startswith(f"{REASON_FORCED_RAMPS}:")
        or nxt.reason.startswith(f"{REASON_POI_SAT}:")
    )
    assert nxt.reason != REASON_HS_OUTLINE


def test_phase_a_is_a_linear_100_percent_ct_sweep() -> None:
    variations = phase_a_variations(LutMode.COLOR_TEMP, _parameters(), _light_info())
    assert all(isinstance(point, ColorTempVariation) and point.bri == 255 for point in variations)
    mireds = [point.ct for point in variations if isinstance(point, ColorTempVariation)]
    assert mireds[:2] == [153, 555]
    assert any(abs(mired - (153 + 555) // 2) <= 25 for mired in mireds)
    assert mireds == sorted(mireds[:2]) + sorted(mireds[2:])


def test_power_mix_adds_the_weaker_channel_up_to_rated() -> None:
    assert power_mix_mired(153, 555, 4.6, 2.5, 4.9) == 201
    assert power_mix_mired(153, 555, 4.6, 2.5, None) is None
    assert power_mix_mired(153, 555, 4.6, 2.5, 0) is None


def test_phase_a_is_a_100_percent_hs_outline() -> None:
    variations = phase_a_variations(LutMode.HS, _parameters(), _light_info())
    assert variations
    assert all(isinstance(point, HsVariation) and point.bri == 255 for point in variations)
    hues = {point.hue for point in variations if isinstance(point, HsVariation) and point.sat == 255}
    assert len(hues) >= 8
    assert any(isinstance(point, HsVariation) and point.sat == 1 for point in variations)
    assert any(isinstance(point, HsVariation) and 1 < point.sat < 255 for point in variations)
    assert {1, 21845, 43690} <= {point.hue for point in variations if isinstance(point, HsVariation)}


def test_36871_hs_white_is_the_hot_envelope_and_mixes_stay_in_band() -> None:
    """Sat-min (all segments) is P_max; full-sat primaries are P_min. Other hues sit inside."""

    points = _load_csv("hs.csv", LutMode.HS)
    at_max = [point for point in points if point.variation.bri == 255]
    hot = max(at_max, key=lambda point: point.watt)
    cold = min(at_max, key=lambda point: point.watt)
    assert isinstance(hot.variation, HsVariation)
    assert isinstance(cold.variation, HsVariation)
    assert hot.variation.sat == 1
    assert hot.watt == max(point.watt for point in at_max)
    assert cold.variation.sat == 255
    assert 4.4 <= hot.watt <= 4.6
    assert 1.2 <= cold.watt <= 1.4

    outside = 0
    for point in at_max:
        band = envelope_watts(points, 255, 1, 255)
        assert band is not None
        low, high = band
        if point.watt < low - 0.05 * hot.watt or point.watt > high + 0.05 * hot.watt:
            outside += 1
    assert outside == 0


def test_36871_ct_midpoint_is_hotter_than_both_ends() -> None:
    """Both-whites-on sits near the numerical mired midpoint, not at an extreme."""

    points = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    at_max = [
        point for point in points if point.variation.bri == 255 and isinstance(point.variation, ColorTempVariation)
    ]
    by_ct = {point.variation.ct: point.watt for point in at_max}
    assert by_ct[153] < by_ct[354]
    assert by_ct[555] < by_ct[153]
    assert by_ct[354] == max(by_ct.values()) or abs(by_ct[354] - max(by_ct.values())) < 0.05


def test_36871_smart_grid_is_much_smaller_than_the_cartesian_product() -> None:
    """Δ=8 coverage walks rails, not the hue×sat×bri / mired×bri product."""

    parameters = _parameters()
    light = _light_info()
    hs = _load_csv("hs.csv", LutMode.HS)
    ct = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    hs_plan = _invent_grid(LutMode.HS, hs, parameters, light)
    ct_plan = _invent_grid(LutMode.COLOR_TEMP, ct, parameters, light)
    hs_cartesian = (
        len(hue_sweep_values(parameters))
        * len(sat_sweep_values(parameters))
        * len(_brightness_walk(parameters, hs=True))
    )
    ct_cartesian = len(color_temp_mireds(parameters, light)) * len(_brightness_walk(parameters, hs=False))
    assert 8 <= len(hs_plan) < hs_cartesian / 2
    assert 6 <= len(ct_plan) < ct_cartesian / 2


def _invent_grid(
    mode: LutMode,
    pool: list[MeasuredPoint],
    parameters: MeasurementParameters,
    light: LightInfo,
) -> set[object]:
    measured: list[MeasuredPoint] = []
    planned: set[object] = set()
    for _ in range(48):
        extra = next_smart_variations(mode, measured, parameters, light)
        if not extra:
            break
        planned.update(extra)
        measured.extend(_nearest_measured(extra, pool))
    return planned


def test_estimate_smart_remaining_counts_ramps_and_coverage_after_the_outline() -> None:
    parameters = _parameters()
    light = _light_info()
    outline = phase_a_variations(LutMode.COLOR_TEMP, parameters, light)
    estimate = estimate_smart_mode(LutMode.COLOR_TEMP, parameters, light)

    remaining = estimate_smart_remaining(LutMode.COLOR_TEMP, parameters, light, outline)

    assert remaining == estimate.discovery + estimate.coverage_cap - len(outline)
    assert remaining > 0


def test_estimate_smart_remaining_does_not_zero_out_when_the_seed_already_has_extras() -> None:
    """A finished or dense parent LUT still needs the current plan's ramps and coverage."""
    parameters = _parameters()
    light = _light_info()
    outline = phase_a_variations(LutMode.COLOR_TEMP, parameters, light)
    first = outline[0]
    assert isinstance(first, ColorTempVariation)
    extras = [ColorTempVariation(bri=max(1, 255 - step * 8), ct=first.ct) for step in range(40)]
    estimate = estimate_smart_mode(LutMode.COLOR_TEMP, parameters, light)
    expected = estimate.discovery + estimate.coverage_cap - len(outline)

    remaining = estimate_smart_remaining(LutMode.COLOR_TEMP, parameters, light, [*outline, *extras])

    assert remaining == expected
    assert remaining > 0


def _cover_mode(mode: LutMode, parameters: MeasurementParameters, light: LightInfo) -> list[MeasuredPoint]:
    pool = _load_csv("color_temp.csv" if mode == LutMode.COLOR_TEMP else "hs.csv", mode)
    measured: list[MeasuredPoint] = []
    for _ in range(64):
        extra = next_smart_variations(mode, measured, parameters, light)
        if not extra:
            break
        measured.extend(_nearest_measured(extra, pool))
    return measured


def test_estimate_smart_remaining_is_zero_when_the_planner_is_done() -> None:
    parameters = _parameters()
    light = _light_info()
    measured = _cover_mode(LutMode.COLOR_TEMP, parameters, light)

    remaining = estimate_smart_remaining(
        LutMode.COLOR_TEMP,
        parameters,
        light,
        [point.variation for point in measured],
        measured_points=measured,
    )

    assert remaining == 0
    assert not smart_planner_has_work(LutMode.COLOR_TEMP, measured, parameters, light)


def test_smart_uninvented_count_skips_a_seed_the_planner_is_done_with() -> None:
    parameters = _parameters()
    light = _light_info()
    measured = _cover_mode(LutMode.COLOR_TEMP, parameters, light)

    extra = smart_uninvented_count(
        [LutMode.COLOR_TEMP],
        [],
        [],
        parameters,
        light,
        measured_by_mode={LutMode.COLOR_TEMP: measured},
    )

    assert extra == 0


def test_estimate_reports_discovery_then_a_coverage_cap() -> None:
    parameters = _parameters()
    estimate = estimate_smart_mode(LutMode.HS, parameters, _light_info())
    discovery = (
        len(phase_a_variations(LutMode.HS, parameters, _light_info()))
        + _forced_ramp_estimate(LutMode.HS, parameters)
        + _poi_sat_estimate(LutMode.HS, parameters)
    )
    assert estimate.discovery == discovery
    assert estimate.coverage_cap > 0
    assert f"discovery {discovery}" in estimate.summary
    assert "forced ramps" in estimate.summary
    assert smart_delta(parameters) == 8.0
    assert smart_border_delta(parameters) == 8.0


def test_estimate_uses_border_delta_for_the_perimeter_cap() -> None:
    tight = estimate_smart_mode(LutMode.HS, _parameters(smart_border_delta=4, smart_delta=20), _light_info())
    loose = estimate_smart_mode(LutMode.HS, _parameters(smart_border_delta=20, smart_delta=20), _light_info())
    assert tight.discovery == loose.discovery
    assert tight.coverage_cap > loose.coverage_cap


def test_coarser_border_traces_fewer_envelope_knots() -> None:
    pool = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    light = _light_info()
    fine = _invent_grid(
        LutMode.COLOR_TEMP,
        pool,
        _parameters(smart_border_delta=4, smart_delta=30, ct_bri_steps=254),
        light,
    )
    coarse = _invent_grid(
        LutMode.COLOR_TEMP,
        pool,
        _parameters(smart_border_delta=30, smart_delta=30, ct_bri_steps=254),
        light,
    )
    assert len(coarse) < len(fine)


def _nearest_measured(wanted: list, pool: list[MeasuredPoint]) -> list[MeasuredPoint]:
    """Pretend the DUT returned the closest already-measured watt for each invented point."""

    by_color: dict[tuple[object, ...], list[MeasuredPoint]] = defaultdict(list)
    for point in pool:
        if isinstance(point.variation, HsVariation):
            key: tuple[object, ...] = ("hs", point.variation.hue, point.variation.sat)
        elif isinstance(point.variation, ColorTempVariation):
            key = ("ct", point.variation.ct)
        else:
            key = ("bri",)
        by_color[key].append(point)
    filled: list[MeasuredPoint] = []
    for variation in wanted:
        if isinstance(variation, HsVariation):
            key = ("hs", variation.hue, variation.sat)
        elif isinstance(variation, ColorTempVariation):
            key = ("ct", variation.ct)
        else:
            key = ("bri",)
        candidates = by_color.get(key)
        if not candidates and isinstance(variation, ColorTempVariation):
            nearest_ct = min(
                (point.variation.ct for point in pool if isinstance(point.variation, ColorTempVariation)),
                key=lambda mired: abs(mired - variation.ct),
            )
            candidates = by_color.get(("ct", nearest_ct))
        elif not candidates and isinstance(variation, HsVariation):
            nearest_hs = min(
                (
                    (point.variation.hue, point.variation.sat)
                    for point in pool
                    if isinstance(point.variation, HsVariation)
                ),
                key=lambda color: abs(color[0] - variation.hue) + abs(color[1] - variation.sat),
            )
            candidates = by_color.get(("hs", *nearest_hs))
        if not candidates:
            candidates = pool
        watt = interpolate_watt(
            [(point.variation.bri, point.watt) for point in candidates],
            variation.bri,
        )
        filled.append(MeasuredPoint(variation=variation, watt=watt))
    return filled


def test_plot_distance_is_normalized() -> None:
    assert plot_distance(1, 0, 255, 0, 1, 255, 4.5) == 100.0
    assert plot_distance(255, 0, 255, 4.5, 1, 255, 4.5) == 100.0


def test_estimated_watt_scales_the_100_percent_sweep_by_a_neighbor_rail() -> None:
    measured = [
        MeasuredPoint(ColorTempVariation(bri=255, ct=153), 4.0),
        MeasuredPoint(ColorTempVariation(bri=128, ct=153), 1.0),
        MeasuredPoint(ColorTempVariation(bri=1, ct=153), 0.2),
        MeasuredPoint(ColorTempVariation(bri=255, ct=300), 3.0),
        MeasuredPoint(ColorTempVariation(bri=255, ct=555), 2.0),
    ]
    guess = estimated_watt(ColorTempVariation(bri=128, ct=300), measured, 1, 255)
    assert guess == 0.75


def test_estimated_watt_does_not_extrapolate_a_sparse_own_rail() -> None:
    measured = [
        MeasuredPoint(ColorTempVariation(bri=255, ct=354), 4.6),
        MeasuredPoint(ColorTempVariation(bri=150, ct=354), 1.0),
        MeasuredPoint(ColorTempVariation(bri=1, ct=354), 0.2),
        MeasuredPoint(ColorTempVariation(bri=255, ct=353), 4.5),
        MeasuredPoint(ColorTempVariation(bri=241, ct=353), 3.8),
    ]
    guess = estimated_watt(ColorTempVariation(bri=151, ct=353), measured, 1, 255)
    assert abs(guess - 4.5 * (1.0 / 4.6)) < 0.05


def test_36871_ct_coverage_fills_plot_space_without_stacking() -> None:
    """Interior points sit ~Δ apart in (bri, watt), not stacked on shared brightness knots."""

    pool = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    parameters = _parameters()
    light = _light_info()
    measured: list[MeasuredPoint] = []
    coverage: list[MeasuredPoint] = []
    for _ in range(48):
        batch = next_smart_batch(LutMode.COLOR_TEMP, measured, parameters, light)
        if not batch.variations:
            break
        found = _nearest_measured(batch.variations, pool)
        measured.extend(found)
        if batch.stage == STAGE_COVERAGE:
            coverage.extend(found)
    p_peak = max(point.watt for point in measured)
    coverage_interior = [point for point in coverage if 1 < point.variation.bri < 255]
    same_bri_stacks = [
        (left.variation, right.variation)
        for i, left in enumerate(coverage_interior)
        for right in coverage_interior[i + 1 :]
        if left.variation.bri == right.variation.bri
        and plot_distance(
            left.variation.bri,
            left.watt,
            right.variation.bri,
            right.watt,
            1,
            255,
            p_peak,
        )
        < 2
    ]
    assert same_bri_stacks == []
    interior = [point for point in measured if 1 < point.variation.bri < 255]
    bris = {point.variation.bri for point in interior}
    assert len(bris) >= 12
    mid_cts = {
        point.variation.ct
        for point in interior
        if isinstance(point.variation, ColorTempVariation) and 80 <= point.variation.bri <= 200
    }
    assert len(mid_cts) >= 3


def test_36871_hs_invents_saturation_and_rides_it() -> None:
    """Watt-gap sats on the hot hue, then the same colors at mid brightness."""

    pool = _load_csv("hs.csv", LutMode.HS)
    planned = _invent_grid(LutMode.HS, pool, _parameters(), _light_info())
    hot_hue = 1
    sats_at_max = {
        point.sat for point in planned if isinstance(point, HsVariation) and point.bri == 255 and point.hue == hot_hue
    }
    sats_at_mid = {
        point.sat
        for point in planned
        if isinstance(point, HsVariation) and 80 <= point.bri <= 200 and point.hue == hot_hue
    }
    assert len(sats_at_max) >= 5
    assert len(sats_at_mid) >= 2


def test_36871_hottest_100_percent_sample_is_near_the_known_peak() -> None:
    pool = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    planned = _invent_grid(LutMode.COLOR_TEMP, pool, _parameters(), _light_info())
    max_bri = [point for point in planned if isinstance(point, ColorTempVariation) and point.bri == 255]
    assert max_bri
    nearest = min(max_bri, key=lambda point: abs(point.ct - 363))
    assert abs(nearest.ct - 363) <= 25


def test_36871_max_bri_watt_gaps_stay_within_border_delta() -> None:
    pool = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    parameters = _parameters()
    light = _light_info()
    measured: list[MeasuredPoint] = []
    for _ in range(48):
        extra = next_smart_variations(LutMode.COLOR_TEMP, measured, parameters, light)
        if not extra:
            break
        measured.extend(_nearest_measured(extra, pool))
    at_max = sorted(
        (point.variation.ct, point.watt)
        for point in measured
        if point.variation.bri == 255 and isinstance(point.variation, ColorTempVariation)
    )
    p_peak = max(watt for _, watt in at_max)
    delta = smart_border_delta(parameters)
    ct_span = max(1, at_max[-1][0] - at_max[0][0])
    over = [
        abs(plot_y(left, p_peak) - plot_y(right, p_peak))
        for (left_ct, left), (right_ct, right) in pairwise(at_max)
        if not ct_watt_jump(
            left_ct,
            left,
            right_ct,
            right,
            ct_span=ct_span,
            p_peak=p_peak,
            delta=delta,
        )
        and abs(plot_y(left, p_peak) - plot_y(right, p_peak)) > delta + 0.5
    ]
    assert over == []


def test_adjacent_mireds_are_not_a_jump_when_watts_match() -> None:
    """A dense 100% rail has many 1-mired neighbours; that is not a hardware cliff."""

    assert not ct_watt_jump(246, 4.32, 247, 4.33, ct_span=400, p_peak=4.7, delta=8)
    assert ct_watt_jump(246, 4.6, 247, 3.2, ct_span=400, p_peak=4.7, delta=8)


def test_dense_valley_is_a_minimum_not_a_discontinuity() -> None:
    measured = [
        MeasuredPoint(ColorTempVariation(bri=255, ct=ct), 4.3 + ((ct - 250) / 50) ** 2 * 0.3) for ct in range(200, 301)
    ]
    reasons = {
        target.color.ct: target.reason
        for target in _ct_forced_targets(measured, _parameters())
        if isinstance(target.color, ColorTempVariation)
    }
    assert not any(reason.startswith("discontinuity") for reason in reasons.values())
    assert reasons[250].startswith("segment minimum at ")


def test_ct_watt_jump_is_not_filled_with_midpoints() -> None:
    """A WW/CW cliff is empty because no mired produces those watts, not because we undersampled."""

    parameters = _parameters()
    measured = [
        MeasuredPoint(ColorTempVariation(bri=255, ct=153), 4.5),
        MeasuredPoint(ColorTempVariation(bri=255, ct=430), 4.5),
        MeasuredPoint(ColorTempVariation(bri=255, ct=450), 3.2),
        MeasuredPoint(ColorTempVariation(bri=255, ct=555), 2.4),
    ]
    extra = _ct_max_bri_watt_gaps(measured, parameters)
    invented = [point.ct for point in extra if isinstance(point, ColorTempVariation)]
    assert all(not (430 < ct < 450) for ct in invented)


def test_replay_continues_the_interrupted_batch() -> None:
    """Resume must finish the queued batch, not re-plan from the denser rail."""

    pool = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    parameters = _parameters()
    light = _light_info()
    measured: list[MeasuredPoint] = []
    interrupted: list = []
    for _ in range(48):
        batch = next_smart_batch(LutMode.COLOR_TEMP, measured, parameters, light)
        if not batch.variations:
            break
        mid_bri = [point for point in batch.variations if 1 < point.bri < 255]
        if len(mid_bri) > 4:
            take = len(batch.variations) // 2
            measured.extend(_nearest_measured(batch.variations[:take], pool))
            interrupted = batch.variations[take:]
            break
        measured.extend(_nearest_measured(batch.variations, pool))
    assert interrupted
    assert replay_smart_remaining(LutMode.COLOR_TEMP, measured, parameters, light) == interrupted


def test_first_rail_batch_is_equidistant_not_a_single_midpoint() -> None:
    pool = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    parameters = _parameters()
    light = _light_info()
    measured: list[MeasuredPoint] = []
    rail_batch: list = []
    for _ in range(48):
        batch = next_smart_batch(LutMode.COLOR_TEMP, measured, parameters, light)
        if not batch.variations:
            break
        bris = {point.bri for point in batch.variations}
        mid = {bri for bri in bris if 1 < bri < 255}
        if len(mid) > 4:
            rail_batch = batch.variations
            break
        measured.extend(_nearest_measured(batch.variations, pool))
    assert rail_batch
    by_color: dict[int, list[int]] = {}
    for point in rail_batch:
        if isinstance(point, ColorTempVariation):
            by_color.setdefault(point.ct, []).append(point.bri)
    longest = max(by_color.values(), key=len)
    ordered = sorted(longest)
    gaps = [right - left for left, right in pairwise(ordered)]
    assert len(ordered) >= 8
    assert max(gaps) / min(gaps) <= 2


def test_dart_fill_is_deterministic_and_replays() -> None:
    pool = _load_csv("color_temp.csv", LutMode.COLOR_TEMP)
    light = _light_info()
    parameters = _parameters(smart_dart=True, smart_delta=16, smart_dart_min_delta=10, smart_border_delta=24)
    first = _invent_grid(LutMode.COLOR_TEMP, pool, parameters, light)
    second = _invent_grid(LutMode.COLOR_TEMP, pool, parameters, light)
    assert first == second
    assert first

    measured: list[MeasuredPoint] = []
    interrupted: list = []
    for _ in range(64):
        extra = next_smart_variations(LutMode.COLOR_TEMP, measured, parameters, light)
        if not extra:
            break
        if measured and extra and extra[0].bri not in {1, 255}:
            interrupted = extra
            break
        measured.extend(_nearest_measured(extra, pool))
    assert interrupted
    assert replay_smart_remaining(LutMode.COLOR_TEMP, measured, parameters, light) == interrupted


def test_estimate_dart_fill_names_the_shrinking_radius() -> None:
    estimate = estimate_smart_mode(
        LutMode.COLOR_TEMP,
        _parameters(smart_dart=True, smart_delta=8, smart_dart_min_delta=2),
        _light_info(),
    )
    assert "dart fill" in estimate.summary
    assert "forced ramps" in estimate.summary
    assert "Δ 8→2" in estimate.summary


# Cool → warm (mired ascending). Mirrors the 100% CT graph: cool plateau with a
# ~4000 K dip, interior global max, a WW/CW cliff, a warm-side kink just after
# the cliff, and a lone 0.2 W outlier that must not spawn a ramp.
_FORCED_CT_RAIL = (
    (153, 4.45),
    (180, 4.48),
    (204, 0.20),
    (220, 4.47),
    (250, 4.35),
    (280, 4.50),
    (320, 4.65),
    (370, 4.80),
    (400, 4.60),
    (410, 3.68),
    (420, 3.70),
    (480, 3.20),
    (555, 2.40),
)


def _forced_ct_measured() -> list[MeasuredPoint]:
    return [MeasuredPoint(ColorTempVariation(bri=255, ct=ct), watt) for ct, watt in _FORCED_CT_RAIL]


def test_ct_forced_colors_picks_peak_cliffs_and_prominent_extrema() -> None:
    colors = _ct_forced_colors(_forced_ct_measured(), _parameters())
    cts = [point.ct for point in colors if isinstance(point, ColorTempVariation)]
    assert 370 in cts
    assert 400 in cts
    assert 410 in cts
    assert 250 in cts
    assert 420 in cts
    assert 555 in cts
    assert 204 not in cts
    assert 153 not in cts


def test_ct_forced_reasons_name_the_feature_and_kelvin() -> None:
    targets = _ct_forced_targets(_forced_ct_measured(), _parameters())
    reasons = {target.color.ct: target.reason for target in targets if isinstance(target.color, ColorTempVariation)}
    assert reasons[400].startswith("discontinuity at ")
    assert reasons[410].startswith("discontinuity at ")
    assert reasons[400].endswith(" K")
    assert reasons[370].startswith("segment maximum at ")
    assert reasons[250].startswith("segment minimum at ")
    assert reasons[420].startswith("segment maximum at ")
    assert reasons[555].startswith("segment minimum at ")


def test_ct_local_extremum_reason_names_kelvin() -> None:
    rail = (
        (150, 5.0),
        (180, 4.0),
        (210, 4.8),
        (240, 3.9),
        (270, 3.0),
    )
    measured = [MeasuredPoint(ColorTempVariation(bri=255, ct=ct), watt) for ct, watt in rail]
    reasons = {
        target.color.ct: target.reason
        for target in _ct_forced_targets(measured, _parameters())
        if isinstance(target.color, ColorTempVariation)
    }
    assert reasons[210].startswith("local maximum at ")
    assert " K" in reasons[210]


def test_one_mired_cliff_keeps_both_sides() -> None:
    """Dedupe merges the same peak, not the two faces of a WW/CW discontinuity."""

    measured = [
        MeasuredPoint(ColorTempVariation(bri=255, ct=ct), watt)
        for ct, watt in (
            (153, 4.50),
            (250, 4.55),
            (369, 4.78),
            (474, 4.60),
            (475, 4.60),
            (476, 3.635),
            (500, 3.50),
            (555, 2.40),
        )
    ]
    targets = _ct_forced_targets(measured, _parameters(smart_border_delta=4, smart_delta=4))
    cts = {target.color.ct for target in targets if isinstance(target.color, ColorTempVariation)}
    reasons = {target.color.ct: target.reason for target in targets if isinstance(target.color, ColorTempVariation)}
    assert 475 in cts
    assert 476 in cts
    assert reasons[475].startswith("discontinuity at ")


def test_coverage_does_not_let_one_rail_cover_another_color() -> None:
    """A packed high-power rail must not retire a 100%-only mid-watt color."""

    parameters = _parameters(smart_delta=4, smart_border_delta=4)
    white = HsVariation(bri=255, hue=1, sat=1)
    peak = HsVariation(bri=255, hue=38230, sat=255)
    measured = [MeasuredPoint(with_brightness(white, bri), 0.4 + 4.1 * (bri / 255) ** 2) for bri in range(1, 256, 8)]
    measured.append(MeasuredPoint(peak, 2.1))
    extra = _coverage_variations([white, peak], measured, parameters, {point.variation for point in measured})
    peak_interior = [
        point.bri for point in extra if isinstance(point, HsVariation) and point.hue == peak.hue and point.bri < 255
    ]
    assert peak_interior


def test_ct_forced_ramps_walk_brightness_at_the_selected_cts() -> None:
    parameters = _parameters(ct_bri_steps=50)
    ramps = _forced_brightness_ramps(LutMode.COLOR_TEMP, _forced_ct_measured(), parameters)
    assert ramps
    cts = {point.ct for point in ramps if isinstance(point, ColorTempVariation)}
    bris = {point.bri for point in ramps}
    assert 370 in cts
    assert 250 in cts
    assert 1 in bris
    assert 255 in bris
    assert len(bris) >= 4
    at_peak = [point.bri for point in ramps if isinstance(point, ColorTempVariation) and point.ct == 370]
    assert at_peak[0] == 1
    assert at_peak == sorted(at_peak)


def test_hs_forced_colors_are_primaries_and_midpoints_at_sat_ends() -> None:
    parameters = _parameters()
    hues = _hs_primary_and_midpoint_hues(parameters)
    assert {1, 21845, 43690} <= set(hues)
    assert len(hues) == 6
    colors = _hs_forced_colors(parameters)
    assert len(colors) == 12
    assert {(point.hue, point.sat) for point in colors if isinstance(point, HsVariation)} == {
        (hue, sat) for hue in hues for sat in (1, 255)
    }
    reasons = [target.reason for target in _hs_forced_targets(parameters)]
    assert any(reason.startswith("red primary at 0°, ") for reason in reasons)
    assert any(reason.startswith("primary midpoint at ") for reason in reasons)
    assert any(reason.endswith("full saturation") for reason in reasons)
    assert any(reason.endswith("white") for reason in reasons)


def test_hs_forced_adds_distant_full_sat_peaks_and_skips_nearby_ones() -> None:
    parameters = _parameters()
    far = 16400
    near = 800
    ring: list[MeasuredPoint] = []
    for hue, watt in (
        (far - 2000, 3.5),
        (far, 4.2),
        (far + 2000, 3.6),
        (near, 4.0),
        (near + 1500, 3.4),
        (40000, 3.3),
    ):
        ring.append(MeasuredPoint(HsVariation(bri=255, hue=hue, sat=255), watt))
    colors = _hs_forced_colors(parameters, ring)
    max_sat = {point.hue for point in colors if isinstance(point, HsVariation) and point.sat == 255}
    assert far in max_sat
    assert near not in max_sat
    extras = [point for point in colors if isinstance(point, HsVariation) and point.hue == far]
    assert extras == [HsVariation(bri=255, hue=far, sat=255)]
    reasons = dict(hs_interest_targets(ring, parameters))
    assert reasons[1].startswith("red primary at 0°")
    assert reasons[far].startswith("local maximum at ")


def test_next_smart_batch_emits_hs_forced_ramps_after_the_outline() -> None:
    parameters = _parameters(hs_bri_steps=64)
    light = _light_info()
    measured = [MeasuredPoint(point, 4.0) for point in phase_a_variations(LutMode.HS, parameters, light)]
    for _ in range(20):
        batch = next_smart_batch(LutMode.HS, measured, parameters, light)
        if not batch.variations:
            break
        mid_bri = [point for point in batch.variations if 1 < point.bri < 255]
        if mid_bri and any(isinstance(point, HsVariation) for point in mid_bri):
            hues = {point.hue for point in batch.variations if isinstance(point, HsVariation)}
            sats = {point.sat for point in batch.variations if isinstance(point, HsVariation)}
            assert len(hues) == 1
            assert len(sats) == 1
            assert batch.reason.startswith(f"{REASON_FORCED_RAMPS}:")
            assert "°" in batch.reason
            assert 1 in {point.bri for point in batch.variations}
            return
        measured.extend(MeasuredPoint(point, 4.0) for point in batch.variations)
    raise AssertionError("smart planner never emitted HS forced ramps")


def test_midpoint_poi_gets_a_saturation_sweep_at_100_percent() -> None:
    parameters = _parameters(hs_sat_divisions=5, min_brightness=255, max_brightness=255)
    light = _light_info()
    outline = phase_a_variations(LutMode.HS, parameters, light)
    measured = [MeasuredPoint(point, 2.0) for point in outline]
    primaries = set(_hs_primary_hues(parameters))
    midpoints = [hue for hue in _hs_primary_and_midpoint_hues(parameters) if hue not in primaries]
    midpoint = midpoints[0]
    already = {
        point.sat for point in outline if isinstance(point, HsVariation) and point.hue == midpoint and point.bri == 255
    }
    missing = set(sat_sweep_values(parameters)) - already
    assert missing
    for _ in range(16):
        batch = next_smart_batch(LutMode.HS, measured, parameters, light)
        if not batch.variations:
            break
        if batch.reason.startswith(f"{REASON_POI_SAT}:") and "midpoint" in batch.reason:
            sats = {point.sat for point in batch.variations if isinstance(point, HsVariation) and point.hue == midpoint}
            assert all(point.bri == 255 for point in batch.variations)
            assert missing <= sats | already
            return
        measured.extend(MeasuredPoint(point, 2.0) for point in batch.variations)
    raise AssertionError("smart planner never swept saturation at a midpoint POI")


def test_local_max_poi_gets_a_saturation_sweep_at_100_percent() -> None:
    parameters = _parameters(hs_sat_divisions=5, min_brightness=255, max_brightness=255)
    light = _light_info()
    far = 16400
    outline = phase_a_variations(LutMode.HS, parameters, light)
    measured = [MeasuredPoint(point, 2.0) for point in outline]
    for hue, watt in ((far - 2000, 3.5), (far, 4.2), (far + 2000, 3.6)):
        measured.append(MeasuredPoint(HsVariation(bri=255, hue=hue, sat=255), watt))
    assert far in dict(hs_interest_targets(measured, parameters))
    missing = set(sat_sweep_values(parameters)) - {parameters.max_sat}
    for _ in range(20):
        batch = next_smart_batch(LutMode.HS, measured, parameters, light)
        if not batch.variations:
            break
        if batch.reason.startswith(f"{REASON_POI_SAT}:") and "local maximum" in batch.reason:
            sats = {point.sat for point in batch.variations if isinstance(point, HsVariation) and point.hue == far}
            assert all(point.bri == 255 for point in batch.variations)
            assert missing <= sats
            return
        measured.extend(MeasuredPoint(point, 2.0) for point in batch.variations)
    raise AssertionError("smart planner never swept saturation at a local-max POI")


def test_estimate_drops_coverage_when_brightness_is_pinned() -> None:
    parameters = _parameters(min_brightness=255, max_brightness=255, smart_delta=4, smart_border_delta=4)
    estimate = estimate_smart_mode(LutMode.HS, parameters, _light_info())
    outline = len(phase_a_variations(LutMode.HS, parameters, _light_info()))
    discovery = outline + _poi_sat_estimate(LutMode.HS, parameters)
    assert estimate.discovery == discovery
    assert estimate.coverage_cap == 0
    assert "pinned" in estimate.summary


def test_pinned_brightness_does_not_densify_a_seed_rail() -> None:
    """Extend-with-255-255 used to walk the parent's 1-255 white rail.

    Perimeter treated inherited dim samples as the brightness axis. With min=max
    the plot-x span collapsed to 1, distances exploded, and it filled every
    integer between the seed knots (130 extra points on one color, 2026-09-13).
    """
    parameters = _parameters(
        min_brightness=255,
        max_brightness=255,
        smart_delta=4,
        smart_border_delta=4,
        hs_bri_steps=16,
    )
    light = _light_info()
    white_rail = [
        1,
        33,
        65,
        86,
        97,
        108,
        118,
        129,
        145,
        161,
        177,
        185,
        193,
        202,
        207,
        216,
        225,
        230,
        233,
        236,
        240,
        245,
        250,
        255,
    ]
    measured = [MeasuredPoint(HsVariation(bri=bri, hue=32767, sat=1), 0.3 + bri * 0.01) for bri in white_rail]
    measured.extend(MeasuredPoint(point, 4.0) for point in phase_a_variations(LutMode.HS, parameters, light))
    seen: set[object] = set()
    for _ in range(12):
        batch = next_smart_batch(LutMode.HS, measured, parameters, light)
        if not batch.variations or batch.variations[0] in seen:
            break
        seen.update(batch.variations)
        dim = [point.bri for point in batch.variations if point.bri < 255]
        assert dim == [], batch.reason
        measured.extend(MeasuredPoint(point, 4.0) for point in batch.variations)
    remaining = estimate_smart_remaining(
        LutMode.HS,
        parameters,
        light,
        {point.variation for point in measured},
        measured_points=measured,
    )
    assert remaining == 0


def test_narrowed_brightness_span_does_not_invent_below_min() -> None:
    parameters = _parameters(min_brightness=200, max_brightness=255, smart_delta=4, smart_border_delta=4)
    light = _light_info()
    measured = [
        MeasuredPoint(HsVariation(bri=bri, hue=32767, sat=1), 0.3 + bri * 0.01) for bri in (1, 33, 65, 129, 193, 255)
    ]
    measured.extend(MeasuredPoint(point, 4.0) for point in phase_a_variations(LutMode.HS, parameters, light))
    batch = next_smart_batch(LutMode.HS, measured, parameters, light)
    assert all(point.bri >= 200 for point in batch.variations)


def test_narrowed_hue_range_does_not_move_or_rediscover_poi_ticks() -> None:
    """A refine that sliced hue and sat must keep the full-wheel RGB ticks and
    the sat=255 local max, not invent new ones at the slider edges / sat=10.
    """
    parameters = _parameters(min_hue=8100, max_hue=40000, max_sat=10)
    primaries = _hs_primary_hues(parameters)
    assert primaries == [1, 21845, 43690]
    far = 16400
    peak_at = {far - 2000: 3.5, far: 4.2, far + 2000: 3.6}
    measured: list[MeasuredPoint] = []
    for hue in (1, 10923, 21845, far - 2000, far, far + 2000, 32768, 43690, 54613):
        measured.append(MeasuredPoint(HsVariation(bri=255, hue=hue, sat=255), peak_at.get(hue, 2.0)))
        measured.append(MeasuredPoint(HsVariation(bri=255, hue=hue, sat=10), 4.3))

    reasons = dict(hs_interest_targets(measured, parameters))
    assert reasons[1].startswith("red primary at 0°")
    assert reasons[21845].startswith("green primary at 120°")
    assert reasons[43690].startswith("blue primary at 240°")
    assert reasons[far].startswith("local maximum at ")
    assert 8100 not in reasons
    assert 40000 not in reasons

    planned = {
        point.hue
        for point in _hs_forced_colors(parameters, measured)
        if isinstance(point, HsVariation)
    }
    assert 1 not in planned
    assert 43690 not in planned
    assert 21845 in planned
    assert far in planned
