from pathlib import Path

from measure.powermeter.errors import StandbyLikeOnError, ZeroReadingError
from measure.runner.on_off_bounds import (
    OnOffBounds,
    closer_to_standby_than_minimum_on,
    load_on_off_bounds,
    minimum_on_from_seed,
    on_load_distinguishable_from_standby,
    save_on_off_bounds,
    seed_supports_standby,
)


def test_midpoint_rejects_the_36871_off_sample() -> None:
    """0.14 W/lamp at 69% was dummy+standby; neighbors at that brightness were ~1.33 W."""

    assert closer_to_standby_than_minimum_on(0.14, standby=0.14, minimum_on=0.30)
    assert not closer_to_standby_than_minimum_on(1.33, standby=0.14, minimum_on=0.30)


def test_midpoint_accepts_the_equal_distance() -> None:
    assert not closer_to_standby_than_minimum_on(0.22, standby=0.14, minimum_on=0.30)


def test_setup_fails_when_on_and_standby_round_to_the_same_hundredth() -> None:
    assert not on_load_distinguishable_from_standby(0.704, 0.701)
    assert on_load_distinguishable_from_standby(0.71, 0.70)


def test_degenerate_bounds_treat_every_reading_as_standby_like() -> None:
    assert closer_to_standby_than_minimum_on(1.0, standby=0.5, minimum_on=0.4)


def test_on_off_bounds_are_per_lamp() -> None:
    bounds = OnOffBounds(standby=0.14, minimum_on=0.30)
    assert closer_to_standby_than_minimum_on(0.14, bounds.standby, bounds.minimum_on)


def test_seed_lowest_on_drops_the_standby_like_outlier() -> None:
    """The 0.14 W 36871 row is dummy+standby; the real dim-on floor is ~0.26 W."""

    assert minimum_on_from_seed([0.14, 0.26, 0.28, 1.33], standby=0.148) == 0.26
    assert not closer_to_standby_than_minimum_on(0.28, standby=0.148, minimum_on=0.26)


def test_standby_like_is_not_a_zero_watt_reading() -> None:
    assert not issubclass(StandbyLikeOnError, ZeroReadingError)


def test_large_seed_rejects_standby_inside_the_on_envelope() -> None:
    on_watts = [0.25 + index * 0.01 for index in range(40)]
    assert seed_supports_standby(on_watts, 0.05)
    assert not seed_supports_standby(on_watts, 1.016)


def test_small_seed_still_accepts_standby_below_the_real_floor() -> None:
    assert seed_supports_standby([0.14, 0.26, 0.28], 0.148)
    assert not seed_supports_standby([0.14, 0.15], 1.016)


def test_on_off_bounds_round_trip(tmp_path: Path) -> None:
    bounds = OnOffBounds(standby=0.05, minimum_on=0.26)
    save_on_off_bounds(tmp_path, bounds)
    assert load_on_off_bounds(tmp_path) == bounds
