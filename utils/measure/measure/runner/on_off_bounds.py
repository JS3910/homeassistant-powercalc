"""Standby vs lowest-on bounds for rejecting off-looking 'on' samples.

Standby is recorded once, with the same settle/OCR/retry path as any other LUT
point, and reused on resume and refine. Lowest on-load comes from the LUT that
already exists — not from a second short probe. An on-state sample that is closer
to that persisted standby than to the LUT floor is residual off, not dim-on.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
from pathlib import Path

ON_OFF_BOUNDS_FILENAME = "on_off_bounds.json"
#: A candidate standby that sits inside the known on-envelope is not standby.
_SEED_STANDBY_USABLE_FRACTION = 0.9
_SEED_STANDBY_FRACTION_MIN_POINTS = 20


@dataclass(frozen=True)
class OnOffBounds:
    """Per-light watts, same unit the LUT stores after dividing by lamp count."""

    standby: float
    minimum_on: float | None = None


def on_load_distinguishable_from_standby(minimum_on: float, standby: float) -> bool:
    """False when the lowest on-load is identical to standby at 0.01 W precision."""

    if minimum_on <= standby:
        return False
    return round(minimum_on, 2) != round(standby, 2)


def minimum_on_from_seed(watts: Iterable[float], standby: float) -> float | None:
    """Lowest on-watt already in the LUT that is distinguishable from ``standby``.

    That is the dim-on floor (0.26 W on 36871). Rows that round to the same
    hundredth as standby (the 0.14 W off-looking outliers) are not on-load.
    """

    usable = sorted(watt for watt in watts if on_load_distinguishable_from_standby(watt, standby))
    return usable[0] if usable else None


def closer_to_standby_than_minimum_on(power: float, standby: float, minimum_on: float) -> bool:
    """True when ``power`` is nearer standby than the established lowest on-load.

    Equal distance (the midpoint) is not closer to standby, so it is accepted.
    """

    if not on_load_distinguishable_from_standby(minimum_on, standby):
        return True
    return abs(power - standby) < abs(power - minimum_on)


def seed_supports_standby(watts: Iterable[float], standby: float) -> bool:
    """False when ``standby`` sits inside the already-measured on-envelope.

    A 3 s leftover-on OCR frame (~1 W/lamp on a 0.27 W dim-on lamp) would make
    ``minimum_on_from_seed`` throw away the real floor and pick the first watt
    just above the bogus standby. A large LUT is the envelope; a fresh off
    reading that most of those points cannot beat is not standby.
    """

    values = [watt for watt in watts]
    if not values:
        return True
    usable = sum(1 for watt in values if on_load_distinguishable_from_standby(watt, standby))
    if len(values) >= _SEED_STANDBY_FRACTION_MIN_POINTS:
        return usable / len(values) >= _SEED_STANDBY_USABLE_FRACTION
    return usable > 0


def load_on_off_bounds(directory: str | Path) -> OnOffBounds | None:
    path = Path(directory) / ON_OFF_BOUNDS_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        standby = float(payload["standby_w_per_lamp"])
    except (KeyError, TypeError, ValueError):
        return None
    raw_minimum = payload.get("minimum_on_w_per_lamp")
    try:
        minimum_on = None if raw_minimum is None else float(raw_minimum)
    except (TypeError, ValueError):
        return None
    return OnOffBounds(standby=standby, minimum_on=minimum_on)


def save_on_off_bounds(directory: str | Path, bounds: OnOffBounds) -> None:
    path = Path(directory) / ON_OFF_BOUNDS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "standby_w_per_lamp": bounds.standby,
                "minimum_on_w_per_lamp": bounds.minimum_on,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
