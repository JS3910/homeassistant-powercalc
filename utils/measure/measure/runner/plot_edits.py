"""Edit, ignore, and delete individual LUT points from a finished session."""

from __future__ import annotations

from collections.abc import Collection, Mapping
import csv
from dataclasses import replace
import gzip
from io import BytesIO, StringIO
import json
from pathlib import Path

from measure.controller.light.const import LutMode
from measure.files import write_json_atomic
from measure.request import LightMeasurementRequest, MeasurementRequest
from measure.runner.light_plan import CSV_HEADERS, Variation
from measure.runner.lut_csv import LutRow, load_mode_rows, write_mode_csv
from measure.visualization.point_meta import parse_point_id, variation_point_id, variation_rail_id

EDITS_FILENAME = "plot_edits.json"


class PlotEditError(ValueError):
    """The requested plot-point action cannot be applied."""


def load_ignored(session_directory: Path) -> set[str]:
    path = session_directory / EDITS_FILENAME
    if not path.is_file() or path.is_symlink():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    ignored = payload.get("ignored") if isinstance(payload, dict) else None
    if not isinstance(ignored, list):
        return set()
    return {str(item) for item in ignored if item}


def load_ignored_near(artifact_directory: Path) -> set[str]:
    """Find ``plot_edits.json`` on the artifact, its output dir, or the session root."""

    for candidate in (artifact_directory, artifact_directory.parent, artifact_directory.parent.parent):
        if (candidate / EDITS_FILENAME).is_file():
            return load_ignored(candidate)
    return set()


def save_ignored(session_directory: Path, ignored: Collection[str]) -> None:
    write_json_atomic(session_directory / EDITS_FILENAME, {"ignored": sorted(set(ignored))})


def apply_plot_action(
    session_directory: Path,
    request: MeasurementRequest,
    point_id: str,
    action: str,
    *,
    watt: float | None = None,
) -> None:
    if not isinstance(request, LightMeasurementRequest):
        raise PlotEditError("Plot edits are only available for light measurements")
    mode, variation = parse_point_id(point_id)
    if action == "ignore":
        save_ignored(session_directory, load_ignored(session_directory) | {point_id})
        return
    if action == "unignore":
        save_ignored(session_directory, load_ignored(session_directory) - {point_id})
        return
    _rewrite_point(session_directory, request, mode, variation, point_id, action, watt=watt)


def _rewrite_point(
    session_directory: Path,
    request: LightMeasurementRequest,
    mode: LutMode,
    variation: Variation,
    point_id: str,
    action: str,
    *,
    watt: float | None,
) -> None:
    csv_path = _mode_csv(session_directory, request, mode)
    rows = load_mode_rows(csv_path, mode)
    existing = rows.get(variation)
    if existing is None:
        raise PlotEditError("That point is not in this session")
    if action == "delete":
        del rows[variation]
        save_ignored(session_directory, load_ignored(session_directory) - {point_id})
    elif action == "edit":
        if watt is None or watt < 0:
            raise PlotEditError("A non-negative watt value is required")
        rows[variation] = replace(existing, watt=float(watt))
    elif action == "fix_outlier":
        left, right = _outlier_neighbors(rows, variation)
        if left is None or right is None:
            raise PlotEditError("This point has no brightness-rail neighbors to interpolate")
        rows[variation] = replace(
            existing,
            watt=_interpolate_rail_watt(left, right, variation.bri),
        )
    else:
        raise PlotEditError(f"Unknown plot action: {action}")
    write_mode_csv(csv_path, mode, rows)


def filter_profile_csv_bytes(payload: bytes, filename: str, ignored: Collection[str]) -> bytes:
    """Drop ignored LUT rows from a profile CSV or ``.csv.gz`` export."""

    if not ignored:
        return payload
    gzipped = filename.endswith(".csv.gz")
    raw = gzip.decompress(payload) if gzipped else payload
    text = raw.decode("utf-8")
    mode = _mode_from_filename(filename)
    if mode is None:
        return payload
    reader = csv.reader(StringIO(text))
    rows = list(reader)
    if not rows or rows[0] != CSV_HEADERS[mode]:
        return payload
    kept = [rows[0]]
    for row in rows[1:]:
        variation = _variation_from_cells(mode, row)
        if variation is None or variation_point_id(mode, variation) not in ignored:
            kept.append(row)
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(kept)
    encoded = buffer.getvalue().encode("utf-8")
    if not gzipped:
        return encoded
    out = BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as handle:
        handle.write(encoded)
    return out.getvalue()


def _outlier_neighbors(
    rows: Mapping[Variation, LutRow],
    variation: Variation,
) -> tuple[LutRow | None, LutRow | None]:
    """Previous/next samples on this brightness rail only (same CT or hue+sat).

    Endpoints use the two nearest points on the same rail so a 100% sample is
    not averaged against other color temperatures at the same brightness.
    """

    rail_id = variation_rail_id(variation.mode, variation)
    rail = [
        row
        for row in rows.values()
        if variation_rail_id(row.variation.mode, row.variation) == rail_id
    ]
    rail.sort(key=lambda row: row.variation.bri)
    index = next((i for i, row in enumerate(rail) if row.variation == variation), None)
    if index is None or len(rail) < 3:
        return None, None
    if 0 < index < len(rail) - 1:
        return rail[index - 1], rail[index + 1]
    if index == 0:
        return rail[1], rail[2]
    return rail[index - 2], rail[index - 1]


def _interpolate_rail_watt(left: LutRow, right: LutRow, bri: int) -> float:
    span = right.variation.bri - left.variation.bri
    if span == 0:
        return (left.watt + right.watt) / 2
    t = (bri - left.variation.bri) / span
    return left.watt + t * (right.watt - left.watt)


def _mode_csv(session_directory: Path, request: LightMeasurementRequest, mode: LutMode) -> Path:
    model_root = request.model_id or "measurement"
    path = session_directory / "output" / model_root / f"{mode.value}.csv"
    if not path.is_file():
        raise PlotEditError(f"No {mode.value} CSV in this session")
    return path


def _mode_from_filename(filename: str) -> LutMode | None:
    stem = filename.removesuffix(".gz").removesuffix(".csv")
    try:
        return LutMode(stem)
    except ValueError:
        return None


def _variation_from_cells(mode: LutMode, row: list[str]) -> Variation | None:
    from measure.runner.light_plan import variation_from_csv_row

    return variation_from_csv_row(row, mode)
