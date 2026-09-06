from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightInfo
from measure.runner.light_plan import (
    ColorTempVariation,
    HsVariation,
    _bisection_order,
    _circular_bisection_order,
    _measurement_range,
    build_light_plan,
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


class TestCircularBisectionOrder:
    """Seeded at evenly spaced points (red/green/blue by default), then breadth-first
    bisection of the gaps between them, wrapping the ring back to the first seed.
    """

    def test_seeds_come_first_and_are_evenly_spaced(self) -> None:
        values = list(range(0, 65535, 2731))  # matches the hs_hue_divisions default of 24 points
        ordered = _circular_bisection_order(values)
        assert ordered[:3] == [values[0], values[len(values) // 3], values[2 * len(values) // 3]]

    def test_visits_every_value_exactly_once(self) -> None:
        values = list(range(0, 65535, 2731))
        ordered = _circular_bisection_order(values)
        assert sorted(ordered) == sorted(values)
        assert len(ordered) == len(set(ordered))

    def test_short_rings_are_unchanged(self) -> None:
        assert _circular_bisection_order([1, 2, 3], seed_count=3) == [1, 2, 3]

    def test_custom_seed_count(self) -> None:
        values = list(range(12))
        ordered = _circular_bisection_order(values, seed_count=4)
        assert ordered[:4] == [0, 3, 6, 9]


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

    def test_hs_sweeps_red_seed_hue_completely_before_green_seed_hue(self) -> None:
        parameters = MeasurementParameters(hs_bri_steps=254, hs_sat_steps=254, hs_hue_divisions=2)
        plan = build_light_plan({LutMode.HS}, parameters, _light_info(), [])
        variations = plan.for_mode(LutMode.HS).variations
        hues = [v.hue for v in variations if isinstance(v, HsVariation)]
        # Only two hue values exist at this coarse a division count (1 and 65535); the
        # first block of points is entirely the first seed hue.
        first_hue = hues[0]
        block_len = sum(1 for h in hues if h == first_hue and hues.index(h) < len(hues))
        assert hues[:block_len] == [first_hue] * block_len
        assert hues[block_len] != first_hue


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
