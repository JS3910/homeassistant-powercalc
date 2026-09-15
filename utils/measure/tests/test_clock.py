from measure.clock import elapsed_seconds, predicted_remaining_seconds, utc_now
import pytest


def test_elapsed_seconds_is_none_until_the_clock_starts() -> None:
    assert elapsed_seconds(None) is None


def test_elapsed_seconds_uses_the_explicit_end_when_given() -> None:
    assert elapsed_seconds("2026-09-07T20:00:00Z", ended_at="2026-09-07T20:02:30Z") == 150


def test_elapsed_seconds_does_not_go_negative() -> None:
    assert elapsed_seconds("2026-09-07T20:02:00Z", ended_at="2026-09-07T20:00:00Z") == 0


def test_utc_now_is_parseable_as_a_start_time() -> None:
    assert elapsed_seconds(utc_now(), ended_at=utc_now()) == pytest.approx(0, abs=1)


@pytest.mark.parametrize(
    "elapsed,completed,total,expected",
    [
        (100.0, 0, 10, None),
        (100.0, 5, 0, None),
        (80.0, 4, 4, 0.0),
        (80.0, 8, 4, 0.0),
        (40.0, 25, 100, 120.0),
        (10.0, 1, 5, 40.0),
    ],
)
def test_predicted_remaining_is_elapsed_times_points_left_over_points_done(
    elapsed: float,
    completed: int,
    total: int,
    expected: float | None,
) -> None:
    remaining = predicted_remaining_seconds(elapsed, completed, total)
    if expected is None:
        assert remaining is None
    else:
        assert remaining == pytest.approx(expected)


def test_predicted_remaining_ignores_resume_seed_rows() -> None:
    """650 seed rows in 11 minutes must not look like 18 s/point."""

    elapsed = 660.0
    completed = 685
    total = 1500
    already = 650
    # Naive completed-as-rate: 660 * 815 / 685 ≈ 13 min. Real rate: 35 new points.
    assert predicted_remaining_seconds(elapsed, completed, total) == pytest.approx(785.0, abs=1)
    remaining = predicted_remaining_seconds(elapsed, completed, total, already)
    assert remaining == pytest.approx(elapsed * (total - completed) / (completed - already))
    assert remaining is not None
    assert remaining > 3 * 3600


def test_predicted_remaining_waits_for_resume_rate() -> None:
    assert predicted_remaining_seconds(660.0, 652, 1500, already=650) is None
