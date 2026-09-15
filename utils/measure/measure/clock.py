from datetime import UTC, datetime, timedelta


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_after(seconds: float) -> str:
    """UTC timestamp ``seconds`` from now, same format as ``utc_now()``."""
    when = datetime.now(UTC) + timedelta(seconds=max(0.0, seconds))
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Parse a ``utc_now()`` timestamp (``Z`` suffix or offset)."""
    return datetime.fromisoformat(value)


def elapsed_seconds(started_at: str | None, *, ended_at: str | None = None) -> float | None:
    """Seconds from ``started_at`` to ``ended_at``, or to now if ``ended_at`` is omitted."""
    if started_at is None:
        return None
    start = parse_utc(started_at)
    end = parse_utc(ended_at) if ended_at is not None else datetime.now(UTC)
    return max(0.0, (end - start).total_seconds())


#: Resume seeds ``completed`` with rows already on disk. Those must not set the rate.
#: A fresh run (``already == 0``) can use the first new point; a resume waits for a
#: handful so one slow sample does not become a multi-hour forecast.
ETA_MIN_NEW_POINTS_FRESH = 1
ETA_MIN_NEW_POINTS_RESUME = 5


def predicted_remaining_seconds(
    elapsed: float,
    completed: int,
    total: int,
    already: int = 0,
) -> float | None:
    """Linear ETA from points taken *this run*, not from seed/resume rows.

    ``None`` until enough new points have finished to have a rate. ``0`` once every
    point is done. Open-ended runs (``total == 0``) have no finite remaining.
    """
    if completed <= 0 or total <= 0:
        return None
    if completed >= total:
        return 0.0
    measured_here = completed - already
    needed = ETA_MIN_NEW_POINTS_FRESH if already <= 0 else ETA_MIN_NEW_POINTS_RESUME
    if measured_here < needed:
        return None
    return elapsed * (total - completed) / measured_here
