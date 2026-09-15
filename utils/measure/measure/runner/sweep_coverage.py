"""Sweep coverage for a light session: planned colour axes vs measured LUT keys.

A sweep is one full inner brightness pass at a fixed colour (one mired, or one hue at
the current saturation). Brightness itself is not an axis on this map.
"""

from collections.abc import Collection, Mapping, Sequence
import csv
from pathlib import Path
from typing import Literal

from measure.controller.light.const import MAX_MIRED, MIN_MIRED, LutMode
from measure.controller.light.controller import LightInfo
from measure.execution import OperatingPoint
from measure.request import LightMeasurementRequest, MeasurementRequest
from measure.runner.light_plan import (
    CSV_HEADERS,
    ColorTempVariation,
    HsVariation,
    Variation,
    build_light_plan,
    variation_from_csv_row,
)

SweepStatus = Literal["done", "current", "partial", "pending", "failed", "inherited"]
SweepTick = dict[str, int | SweepStatus]
SweepCoverage = dict[str, list[SweepTick]]

_COLOR_MODES = frozenset({LutMode.COLOR_TEMP, LutMode.HS})


def load_measured_variations(model_root: Path, modes: Collection[LutMode]) -> list[Variation]:
    """Parse complete LUT rows from each mode CSV under ``model_root``."""

    variations: list[Variation] = []
    for mode in modes:
        path = model_root / f"{mode.value}.csv"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
        except OSError:
            continue
        if not rows or rows[0] != CSV_HEADERS[mode]:
            continue
        for row in rows[1:]:
            variation = variation_from_csv_row(row, mode)
            if variation is not None:
                variations.append(variation)
    return variations


def has_complete_lut_row(model_root: Path, modes: Collection[LutMode]) -> bool:
    return bool(load_measured_variations(model_root, modes))


def build_sweep_coverage(
    request: MeasurementRequest,
    measured: Sequence[Variation],
    operating_point: OperatingPoint | None = None,
) -> SweepCoverage | None:
    """Return per-axis ticks, or ``None`` when the session is not a light measurement."""

    if not isinstance(request, LightMeasurementRequest):
        return None
    plan_modes = request.modes & _COLOR_MODES
    if not plan_modes and not _extra_color_keys(measured):
        return {}

    light_info = _coverage_light_info(measured, operating_point)
    plan_variations = _plan_variations(request, light_info)
    current = _current_colour(operating_point)
    coverage: SweepCoverage = {}
    if LutMode.COLOR_TEMP in plan_modes or any(isinstance(item, ColorTempVariation) for item in measured):
        ticks = _color_temp_ticks(plan_variations, measured, current)
        if ticks:
            coverage["color_temp"] = ticks
    if LutMode.HS in plan_modes or any(isinstance(item, HsVariation) for item in measured):
        hue_ticks = _hue_ticks(plan_variations, measured, current)
        sat_ticks = _saturation_ticks(plan_variations, measured, current)
        if hue_ticks:
            coverage["hue"] = hue_ticks
        if sat_ticks:
            coverage["saturation"] = sat_ticks
    return coverage


def _plan_variations(request: LightMeasurementRequest, light_info: LightInfo) -> list[Variation]:
    plan_modes = request.modes & _COLOR_MODES
    if not plan_modes:
        return []
    return build_light_plan(plan_modes, request.parameters, light_info).variations


def _coverage_light_info(measured: Sequence[Variation], operating_point: OperatingPoint | None) -> LightInfo:
    """Prefer the observed mired span once both ends exist; otherwise the catalog default."""

    mireds = [variation.ct for variation in measured if isinstance(variation, ColorTempVariation)]
    current = _current_colour(operating_point).get("color_temp_mired")
    if current is not None:
        mireds.append(current)
    unique = set(mireds)
    if len(unique) >= 2:
        return LightInfo("unknown", min_mired=min(unique), max_mired=max(unique))
    return LightInfo("unknown", min_mired=MIN_MIRED, max_mired=MAX_MIRED)


def _current_colour(operating_point: OperatingPoint | None) -> dict[str, int]:
    if operating_point is None or operating_point.get("type") != "light" or not operating_point.get("on"):
        return {}
    point: Mapping[str, object] = operating_point
    colour: dict[str, int] = {}
    for key in ("color_temp_mired", "hue", "saturation"):
        value = point.get(key)
        if isinstance(value, int):
            colour[key] = value
    return colour


def _color_temp_ticks(
    planned: Sequence[Variation],
    measured: Sequence[Variation],
    current: Mapping[str, int],
) -> list[SweepTick]:
    required: dict[int, set[int]] = {}
    present: dict[int, set[int]] = {}
    for variation in planned:
        if isinstance(variation, ColorTempVariation):
            required.setdefault(variation.ct, set()).add(variation.bri)
    for variation in measured:
        if isinstance(variation, ColorTempVariation):
            present.setdefault(variation.ct, set()).add(variation.bri)
    values = _axis_values(required, present, current.get("color_temp_mired"))
    return [
        _tick(value, required.get(value), present.get(value, set()), current.get("color_temp_mired"))
        for value in values
    ]


def _hue_ticks(
    planned: Sequence[Variation],
    measured: Sequence[Variation],
    current: Mapping[str, int],
) -> list[SweepTick]:
    required: dict[int, set[tuple[int, int]]] = {}
    present: dict[int, set[tuple[int, int]]] = {}
    for variation in planned:
        if isinstance(variation, HsVariation):
            required.setdefault(variation.hue, set()).add((variation.sat, variation.bri))
    for variation in measured:
        if isinstance(variation, HsVariation):
            present.setdefault(variation.hue, set()).add((variation.sat, variation.bri))
    values = _axis_values(required, present, current.get("hue"))
    return [_tick(value, required.get(value), present.get(value, set()), current.get("hue")) for value in values]


def _saturation_ticks(
    planned: Sequence[Variation],
    measured: Sequence[Variation],
    current: Mapping[str, int],
) -> list[SweepTick]:
    required: dict[int, set[tuple[int, int]]] = {}
    present: dict[int, set[tuple[int, int]]] = {}
    for variation in planned:
        if isinstance(variation, HsVariation):
            required.setdefault(variation.sat, set()).add((variation.hue, variation.bri))
    for variation in measured:
        if isinstance(variation, HsVariation):
            present.setdefault(variation.sat, set()).add((variation.hue, variation.bri))
    values = _axis_values(required, present, current.get("saturation"))
    return [_tick(value, required.get(value), present.get(value, set()), current.get("saturation")) for value in values]


def _axis_values(
    required: Mapping[int, Collection[object]],
    present: Mapping[int, Collection[object]],
    current: int | None,
) -> list[int]:
    """Planned values first (plan order), then extra CSV / current-point values."""

    ordered = list(dict.fromkeys(required))
    extras = [value for value in present if value not in required]
    if current is not None and current not in required and current not in present:
        extras.append(current)
    extras.sort()
    return [*ordered, *extras]


def _tick(
    value: int,
    required: Collection[object] | None,
    present: Collection[object],
    current: int | None,
) -> SweepTick:
    required_set = set(required) if required is not None else None
    present_set = set(present)
    if required_set is None:
        done = bool(present_set)
        started = done
        inherited = done
    else:
        done = required_set <= present_set
        started = bool(required_set & present_set)
        inherited = False
    if current == value:
        # Still on this colour, even if every inner brightness is already written.
        # A check means finished-and-left; the live tick is in progress.
        status: SweepStatus = "current"
    elif inherited:
        # Measured under an earlier plan (refine seed) and not in this run's grid.
        status = "inherited"
    elif done:
        status = "done"
    elif started:
        status = "partial"
    else:
        status = "pending"
    return {"value": value, "status": status}


def _extra_color_keys(measured: Sequence[Variation]) -> bool:
    return any(isinstance(item, ColorTempVariation | HsVariation) for item in measured)
