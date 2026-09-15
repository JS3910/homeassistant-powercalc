from measure.controller.light.capabilities import kelvin_to_mired
from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightInfo
from measure.runner.light_plan import (
    ColorTempVariation,
    HsVariation,
    HUE_MODULO,
    Variation,
    _bisection_order,
    _circular_hue_values,
    _circular_trisection_order,
    _divisions_range,
    _hue_grid,
    _measurement_range,
    _saturation_order,
    _saturation_sweep_values,
    build_light_plan,
    premeasure_emitter_bounds,
)
from measure.tuning import MeasurementParameters


def _light_info(*, min_mired: int = 153, max_mired: int = 500) -> LightInfo:
    return LightInfo(model_id="test", min_mired=min_mired, max_mired=max_mired)


class TestBisectionOrder:
    """Linear bisection: both ends first, then breadth-first midpoints of each half.

    Used for color temperature, where the two ends are physically the two LED emitters
    (warm/cold white) and everything between is just their blend.
    """

    def test_both_ends_come_first(self) -> None:
        assert _bisection_order([10, 20, 30, 40, 50])[:2] == [10, 50]

    def test_visits_every_value_exactly_once(self) -> None:
        values = list(range(153, 501, 10))
        ordered = _bisection_order(values)
        assert sorted(ordered) == sorted(values)
        assert len(ordered) == len(set(ordered))

    def test_short_ranges_are_unchanged(self) -> None:
        assert _bisection_order([]) == []
        assert _bisection_order([5]) == [5]
        assert _bisection_order([5, 9]) == [5, 9]

    def test_bisects_breadth_first_not_depth_first(self) -> None:
        # 9 values, indices 0..8: ends first (0, 8), then the midpoint of the whole range
        # (4), then the midpoints of *both* remaining halves (2 and 6) before going any
        # deeper -- this is what "repeat that for the two halves" means.
        values = list(range(9))
        ordered = _bisection_order(values)
        assert ordered[:5] == [0, 8, 4, 2, 6]


class TestCircularTrisectionOrder:
    """Seeded at evenly spaced points (red/green/blue by default), then breadth-first
    *trisection* of the gaps between them, wrapping the ring back to the first seed --
    matching `hs_hue_divisions` being validated as a multiple of 3.
    """

    def test_seeds_come_first_and_are_evenly_spaced(self) -> None:
        values = list(range(0, 65535, 2731))  # matches the hs_hue_divisions default of 24 points
        ordered = _circular_trisection_order(values)
        assert ordered[:3] == [values[0], values[len(values) // 3], values[2 * len(values) // 3]]

    def test_visits_every_value_exactly_once(self) -> None:
        values = list(range(0, 65535, 2731))
        ordered = _circular_trisection_order(values)
        assert sorted(ordered) == sorted(values)
        assert len(ordered) == len(set(ordered))

    def test_short_rings_are_unchanged(self) -> None:
        assert _circular_trisection_order([1, 2, 3], seed_count=3) == [1, 2, 3]

    def test_custom_seed_count(self) -> None:
        values = list(range(12))
        ordered = _circular_trisection_order(values, seed_count=4)
        assert ordered[:4] == [0, 3, 6, 9]

    def test_trisects_breadth_first_not_depth_first(self) -> None:
        # 9 evenly spaced hue values: 3 seeds first, then both trisection points of each
        # of the 3 seed-to-seed gaps before going any deeper.
        values = list(range(9))
        ordered = _circular_trisection_order(values)
        assert ordered[:3] == [0, 3, 6]
        assert sorted(ordered[3:9]) == [1, 2, 4, 5, 7, 8]


class TestModeSweepOrder:
    """End-to-end: `build_light_plan` actually applies bisection order to the real grid."""

    def test_color_temp_sweeps_min_then_max_mired_before_any_midpoint(self) -> None:
        # 8 divisions over the default 153-500 mired range works out to the same ~50-mired
        # spacing the old fixed step used to produce.
        parameters = MeasurementParameters(ct_bri_steps=50, ct_mired_divisions=8)
        plan = build_light_plan({LutMode.COLOR_TEMP}, parameters, _light_info(), [])
        variations = plan.for_mode(LutMode.COLOR_TEMP).variations
        cts = [v.ct for v in variations if isinstance(v, ColorTempVariation)]
        bri_points_per_mired = len(_measurement_range(parameters, 1, 255, 50))

        assert cts[0] == 153  # min mired, at min brightness
        # Every brightness point is swept at the minimum mired before any other mired
        # value appears -- "min color temp 0%->100% brightness" happens as one block.
        first_block = cts[:bri_points_per_mired]
        assert set(first_block) == {153}
        second_block = cts[bri_points_per_mired : 2 * bri_points_per_mired]
        assert set(second_block) == {500}  # max mired next, also as one full block

    def test_color_temp_full_block_is_min_then_max_then_midpoints(self) -> None:
        # 3 divisions over 100-300 mired gives exactly [100, 200, 300].
        parameters = MeasurementParameters(min_brightness=1, max_brightness=255, ct_bri_steps=254, ct_mired_divisions=3)
        plan = build_light_plan({LutMode.COLOR_TEMP}, parameters, _light_info(min_mired=100, max_mired=300), [])
        variations = plan.for_mode(LutMode.COLOR_TEMP).variations
        # With ct_bri_steps == the full brightness span, each mired value gets exactly the
        # two brightness endpoints (1 and 255) before moving to the next mired value.
        mired_sequence = [variations[i].ct for i in range(0, len(variations), 2)]
        assert mired_sequence[:3] == [100, 300, 200]  # min, max, then their midpoint

    def test_color_temp_clamps_to_the_requested_kelvin_window(self) -> None:
        # Light is 153–500 mired (≈6535–2000 K). Ask only for the warm half.
        parameters = MeasurementParameters(
            min_kelvin=2000,
            max_kelvin=3200,
            ct_bri_steps=254,
            ct_mired_divisions=2,
        )
        plan = build_light_plan({LutMode.COLOR_TEMP}, parameters, _light_info(), [])
        mireds = {variation.ct for variation in plan.for_mode(LutMode.COLOR_TEMP).variations}
        assert min(mireds) == kelvin_to_mired(3200)
        assert max(mireds) == kelvin_to_mired(2000)
        assert min(mireds) > 153

    def test_hs_sweeps_red_seed_hue_completely_before_green_seed_hue(self) -> None:
        # Pinned to a single saturation level (min == max) so this test is only about hue
        # ordering -- saturation's own outer-loop behavior has its own test below.
        parameters = MeasurementParameters(
            hs_bri_steps=254,
            hs_hue_divisions=2,
            min_sat=255,
            max_sat=255,
        )
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        variations = plan.for_mode(LutMode.HS).variations
        hues = [v.hue for v in variations if isinstance(v, HsVariation)]
        # Two opposite hues on the wheel (not the wrap-around red pair); the first
        # block of points is entirely the first seed hue.
        first_hue = hues[0]
        block_len = sum(1 for h in hues if h == first_hue and hues.index(h) < len(hues))
        assert hues[:block_len] == [first_hue] * block_len
        assert hues[block_len] != first_hue

    def test_hs_sweeps_max_saturation_before_any_lower_saturation(self) -> None:
        parameters = MeasurementParameters(hs_bri_steps=254, hs_sat_divisions=3, hs_hue_divisions=2)
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        variations = plan.for_mode(LutMode.HS).variations
        sats = [v.sat for v in variations if isinstance(v, HsVariation)]
        # 3 saturation sweeps (255, 128, 1): every hue (and every brightness point
        # within it) is swept at full saturation before saturation drops.
        bri_points_per_hue = len(_measurement_range(parameters, 1, 255, 254))
        hue_count = 2
        max_sat_block = sats[: bri_points_per_hue * hue_count]
        assert set(max_sat_block) == {255}
        assert sats[bri_points_per_hue * hue_count] != 255

    def test_hs_sweeps_a_full_brightness_range_before_hue_or_saturation_changes(self) -> None:
        # Power only really differs at the higher end of brightness, so a full 0-100%
        # sweep must complete for every (hue, saturation) combination -- see
        # _variations_for_mode -- rather than being interrupted partway by a hue or
        # saturation change.
        parameters = MeasurementParameters(hs_bri_steps=50, hs_sat_divisions=1, hs_hue_divisions=2)
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        variations = plan.for_mode(LutMode.HS).variations
        bri_points_per_combo = len(_measurement_range(parameters, 1, 255, 50))
        first_combo = variations[:bri_points_per_combo]
        assert [v.bri for v in first_combo] == _measurement_range(parameters, 1, 255, 50)
        assert {(v.hue, v.sat) for v in first_combo} == {(first_combo[0].hue, first_combo[0].sat)}


class TestSaturationSweepValues:
    """Saturation is a sweep count whose first point is always full saturation."""

    def test_one_sweep_is_full_saturation_only(self) -> None:
        assert _saturation_sweep_values(MeasurementParameters(), 1, 255, 1) == [255]

    def test_two_sweeps_are_full_saturation_then_the_midpoint(self) -> None:
        assert _saturation_sweep_values(MeasurementParameters(), 1, 255, 2) == [255, 128]

    def test_three_sweeps_are_full_then_mid_then_unsaturated(self) -> None:
        assert _saturation_sweep_values(MeasurementParameters(), 1, 255, 3) == [255, 128, 1]

    def test_five_sweeps_bisect_after_the_three_informative_points(self) -> None:
        # Even spacing of 5 points on 1..255 is 1, 65, 129, 193, 255. Mid is 129
        # (closest to 128). After max/mid/min, bisection of the leftover quarters.
        assert _saturation_sweep_values(MeasurementParameters(), 1, 255, 5) == [255, 129, 1, 65, 193]


class TestRedundantBrightnessModeIsDropped:
    """A plain-brightness pass has no defined color/temp to hold constant -- it just
    measures whatever the light already happened to be left set to. Once a color mode is
    also being measured, that mode's own sweep already covers the full brightness range
    at every color point, so the standalone pass is dropped rather than run first against
    an undefined color state. Confirmed 2026-09-06 during hardware testing: without this,
    a brightness+color_temp+hs request ran its brightness pass against whatever color the
    light was last left in from an earlier test (solid blue), not anything repeatable.
    """

    def test_brightness_is_dropped_alongside_color_temp(self) -> None:
        parameters = MeasurementParameters()
        plan = build_light_plan({LutMode.BRIGHTNESS, LutMode.COLOR_TEMP}, parameters, _light_info(), [])

        assert [mode_plan.mode for mode_plan in plan.modes] == [LutMode.COLOR_TEMP]

    def test_brightness_is_dropped_alongside_hs(self) -> None:
        parameters = MeasurementParameters()
        plan = build_light_plan({LutMode.BRIGHTNESS, LutMode.HS}, parameters, _light_info(), [])

        assert [mode_plan.mode for mode_plan in plan.modes] == [LutMode.HS]

    def test_brightness_is_dropped_when_all_three_are_requested(self) -> None:
        parameters = MeasurementParameters()
        plan = build_light_plan(
            {LutMode.BRIGHTNESS, LutMode.COLOR_TEMP, LutMode.HS},
            parameters,
            _light_info(),
            [],
        )

        assert [mode_plan.mode for mode_plan in plan.modes] == [LutMode.COLOR_TEMP, LutMode.HS]

    def test_brightness_alone_is_kept_when_no_color_mode_is_requested(self) -> None:
        parameters = MeasurementParameters()
        plan = build_light_plan({LutMode.BRIGHTNESS}, parameters, _light_info(), [])

        assert [mode_plan.mode for mode_plan in plan.modes] == [LutMode.BRIGHTNESS]
        assert plan.for_mode(LutMode.BRIGHTNESS).variations  # still does real work


class TestBrightnessBisectionAllAndDescending:
    """Bisect/All choose the brightness *set*. The walk on a rail is always linear."""

    def test_hs_bri_steps_32_is_a_native_step_not_32_sweeps(self) -> None:
        parameters = MeasurementParameters(hs_bri_steps=32, hs_sat_divisions=1, hs_hue_divisions=3)
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        brightness = [variation.bri for variation in plan.for_mode(LutMode.HS).variations]
        expected = _measurement_range(parameters, 1, 255, 32)
        assert brightness[: len(expected)] == expected
        assert expected[0] == 1
        assert len(expected) == 9

    def test_bisection_treats_the_number_as_a_sweep_count(self) -> None:
        parameters = MeasurementParameters(
            hs_bri_steps=5,
            hs_bri_bisection=True,
            hs_sat_divisions=1,
            hs_hue_divisions=3,
        )
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        expected = sorted(_divisions_range(parameters, 1, 255, 5))
        first_sweep = plan.for_mode(LutMode.HS).variations[: len(expected)]
        assert [variation.bri for variation in first_sweep] == expected
        assert expected[0] == 1
        assert len(expected) != 9

    def test_all_walks_every_integer_low_to_high(self) -> None:
        parameters = MeasurementParameters(
            min_brightness=10,
            max_brightness=14,
            hs_bri_all=True,
            hs_sat_divisions=1,
            hs_hue_divisions=3,
        )
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        first_sweep = plan.for_mode(LutMode.HS).variations[:5]
        assert [variation.bri for variation in first_sweep] == [10, 11, 12, 13, 14]

    def test_linear_descending_is_max_to_min(self) -> None:
        parameters = MeasurementParameters(bri_bri_steps=50, brightness_descending=True)
        plan = build_light_plan({LutMode.BRIGHTNESS}, parameters, _light_info(), [])
        expected = list(reversed(_measurement_range(parameters, 1, 255, 50)))
        assert [variation.bri for variation in plan.for_mode(LutMode.BRIGHTNESS).variations] == expected
        assert expected[0] == 255

    def test_bisection_descending_walks_the_chosen_points_high_to_low(self) -> None:
        parameters = MeasurementParameters(bri_bri_steps=5, bri_bri_bisection=True, brightness_descending=True)
        plan = build_light_plan({LutMode.BRIGHTNESS}, parameters, _light_info(), [])
        expected = list(reversed(sorted(_divisions_range(parameters, 1, 255, 5))))
        assert [variation.bri for variation in plan.for_mode(LutMode.BRIGHTNESS).variations] == expected
        assert expected[0] == 255
        assert expected[-1] == 1

    def test_linear_ascending_starts_at_min_brightness(self) -> None:
        parameters = MeasurementParameters(bri_bri_steps=50)
        plan = build_light_plan({LutMode.BRIGHTNESS}, parameters, _light_info(), [])
        brightness = [variation.bri for variation in plan.for_mode(LutMode.BRIGHTNESS).variations]
        walk = _measurement_range(parameters, 1, 255, 50)
        assert brightness == walk
        assert brightness[0] == 1

    def test_color_temp_bisects_rails_and_walks_each_rail_high_to_low(self) -> None:
        parameters = MeasurementParameters(
            min_brightness=1,
            max_brightness=8,
            ct_bri_all=True,
            brightness_descending=True,
            ct_mired_divisions=4,
            min_kelvin=2000,
            max_kelvin=4000,
        )
        info = _light_info(min_mired=250, max_mired=500)
        plan = build_light_plan({LutMode.COLOR_TEMP}, parameters, info, [])
        variations = plan.for_mode(LutMode.COLOR_TEMP).variations
        rail_len = 8
        rails = [variations[index : index + rail_len] for index in range(0, len(variations), rail_len)]
        assert [variation.bri for variation in rails[0]] == [8, 7, 6, 5, 4, 3, 2, 1]
        mireds = [rail[0].ct for rail in rails]
        assert mireds == _bisection_order(sorted(set(mireds)))
        assert len(mireds) >= 3
        assert abs(mireds[1] - mireds[0]) == max(mireds) - min(mireds)

    def test_new_run_premeasures_emitter_bounds_then_walks_each_rail(self) -> None:
        parameters = MeasurementParameters(
            min_brightness=1,
            max_brightness=8,
            ct_bri_all=True,
            brightness_descending=True,
            ct_mired_divisions=2,
            min_kelvin=2000,
            max_kelvin=4000,
        )
        info = _light_info(min_mired=250, max_mired=500)
        plan = build_light_plan({LutMode.COLOR_TEMP}, parameters, info, [])
        variations = premeasure_emitter_bounds(plan.for_mode(LutMode.COLOR_TEMP).variations)
        assert [(point.ct, point.bri) for point in variations[:4]] == [
            (250, 8),
            (250, 1),
            (500, 8),
            (500, 1),
        ]
        rest = variations[4:]
        rails = [rest[index : index + 6] for index in range(0, len(rest), 6)]
        assert [point.bri for point in rails[0]] == [7, 6, 5, 4, 3, 2]
        assert [point.bri for point in rails[1]] == [7, 6, 5, 4, 3, 2]

    def test_hs_premeasure_hits_each_rgb_primary_at_full_sat(self) -> None:
        parameters = MeasurementParameters(
            min_brightness=1,
            max_brightness=10,
            hs_bri_all=True,
            hs_sat_divisions=1,
            hs_hue_divisions=3,
        )
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        variations = premeasure_emitter_bounds(plan.for_mode(LutMode.HS).variations)
        diagnostics = variations[:6]
        assert {point.sat for point in diagnostics} == {255}
        assert {point.bri for point in diagnostics} == {1, 10}
        assert len({point.hue for point in diagnostics}) == 3
        assert all(isinstance(point, HsVariation) for point in diagnostics)

    def test_premeasure_is_a_no_op_when_brightness_has_no_span(self) -> None:
        variations = [Variation(10)]
        assert premeasure_emitter_bounds(variations) == variations


class TestAxisAllOrder:
    def test_mired_all_uses_bisection_order(self) -> None:
        parameters = MeasurementParameters(ct_mired_all=True, ct_bri_steps=254)
        plan = build_light_plan({LutMode.COLOR_TEMP}, parameters, _light_info(min_mired=100, max_mired=104), [])
        mireds = [variation.ct for variation in plan.for_mode(LutMode.COLOR_TEMP).variations[::2]]
        assert mireds == _bisection_order([100, 101, 102, 103, 104])

    def test_saturation_all_uses_saturation_order(self) -> None:
        parameters = MeasurementParameters(
            hs_sat_all=True,
            min_sat=1,
            max_sat=5,
            hs_hue_divisions=3,
            hs_bri_steps=254,
        )
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        expected = _saturation_order([1, 2, 3, 4, 5])
        sats = [variation.sat for variation in plan.for_mode(LutMode.HS).variations]
        block = len(sats) // len(expected)
        assert [sats[index * block] for index in range(len(expected))] == expected

    def test_hue_all_uses_circular_trisection_order(self) -> None:
        parameters = MeasurementParameters(
            hs_hue_all=True,
            min_hue=1,
            max_hue=9,
            hs_sat_divisions=1,
            hs_bri_steps=254,
        )
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        expected = _circular_trisection_order(list(range(1, 10)))
        hues = [variation.hue for variation in plan.for_mode(LutMode.HS).variations]
        block = len(hues) // len(expected)
        assert [hues[index * block] for index in range(len(expected))] == expected


class TestHueRing:
    """A full wheel is a ring: N sweeps are N unique hues, never doubled red."""

    def test_six_full_wheel_sweeps_are_six_unique_hues(self) -> None:
        hues = _circular_hue_values(1, 6)
        assert len(hues) == 6
        assert len(set(hues)) == 6
        assert not (1 in hues and 65535 in hues)

    def test_three_full_wheel_sweeps_are_red_green_blue(self) -> None:
        hues = _circular_hue_values(1, 3)
        assert len(hues) == 3
        assert hues[0] == 1
        # 120° and 240° on a 65536-wide ring, starting at 1.
        assert hues[1] == 1 + round(HUE_MODULO / 3)
        assert hues[2] == 1 + round(2 * HUE_MODULO / 3)

    def test_plan_does_not_measure_wrap_around_red_twice(self) -> None:
        parameters = MeasurementParameters(hs_hue_divisions=6, hs_sat_divisions=1, hs_bri_steps=254)
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        hues = {variation.hue for variation in plan.for_mode(LutMode.HS).variations}
        assert len(hues) == 6
        assert not (1 in hues and 65535 in hues)

    def test_partial_arc_still_includes_both_linear_ends(self) -> None:
        parameters = MeasurementParameters(fast_test_mode=False)
        assert _hue_grid(parameters, 1000, 4000, 3, all_values=False) == _divisions_range(parameters, 1000, 4000, 3)

    def test_fast_test_full_wheel_uses_opposite_hues_not_the_wrap_pair(self) -> None:
        parameters = MeasurementParameters(fast_test_mode=True)
        hues = _hue_grid(parameters, 1, 65535, 6, all_values=False)
        assert hues == [1, 1 + HUE_MODULO // 2]
        assert 65535 not in hues

    def test_hue_all_on_the_full_wheel_drops_the_wrap_end(self) -> None:
        parameters = MeasurementParameters()
        hues = _hue_grid(parameters, 1, 65535, 6, all_values=True)
        assert hues[0] == 1
        assert hues[-1] == 65534
        assert 65535 not in hues
