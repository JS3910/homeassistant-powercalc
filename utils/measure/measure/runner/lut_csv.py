"""Keyed load, quality-ranked union, and stable rewrite of light LUT CSVs.

Cartesian resume still walks the last complete row of a frozen plan. Smart resume
replays the planner from every complete row instead. Merge and refine need a
different primitive: every complete row as ``dict[Variation, LutRow]``, unioned by LUT
key, with clashes decided by sample quality rather than session order.

``*.raw.jsonl`` stays diagnostic. The CSV watt is the source of truth; quality is read
from the raw lines that share that row's mode and variation.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from contextlib import suppress
import csv
from dataclasses import dataclass
from enum import StrEnum
import gzip
import json
from pathlib import Path
import shutil
from typing import Literal

from measure.controller.light.const import LutMode
from measure.runner.light_plan import (
    CSV_HEADERS,
    ColorTempVariation,
    EffectVariation,
    HsVariation,
    Variation,
    variation_from_csv_row,
)


class ReplaceReason(StrEnum):
    SETTLE = "settle"
    SAMPLES = "samples"
    WITNESS = "witness"
    NEWER = "newer"


#: Settled plateau beats a fixed wait / missing settle field, which beats a timed-out wait.
_SETTLE_SETTLED = 2
_SETTLE_UNKNOWN = 1
_SETTLE_TIMED_OUT = 0


@dataclass(frozen=True)
class LutQuality:
    settle_rank: int
    reading_count: int
    witness_agrees: bool | None
    timestamp: float | None
    session_updated_at: str | None = None


@dataclass(frozen=True)
class LutRow:
    variation: Variation
    watt: float
    quality: LutQuality


@dataclass(frozen=True)
class ModeUnionPreview:
    kept: int
    added: int
    replaced: int
    replaced_reasons: dict[str, int]


def missing_variations(
    plan: Sequence[Variation],
    measured: Mapping[Variation, object] | Collection[Variation],
) -> list[Variation]:
    """Return plan points that have no measured row yet, preserving plan order."""
    keys = set(measured)
    return [variation for variation in plan if variation not in keys]


def load_mode_rows(
    path: Path | str,
    mode: LutMode,
    *,
    session_updated_at: str | None = None,
) -> dict[Variation, LutRow]:
    """Load every complete mode-CSV row, dropping torn trailing lines the way resume does.

    Quality is joined from the sibling ``*.raw.jsonl`` when present. A CSV row with no
    raw lines counts as one reading of unknown settle — we do not invent a sample count
    from the session request.
    """
    csv_path = Path(path)
    if not csv_path.is_file() or csv_path.is_symlink():
        return {}
    try:
        raw_text = csv_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    rows = list(csv.reader(raw_text.splitlines()))
    if not rows or rows[0] != CSV_HEADERS[mode]:
        return {}

    valid_row_count = len(rows)
    while valid_row_count > 1 and variation_from_csv_row(rows[valid_row_count - 1], mode) is None:
        valid_row_count -= 1

    watt_index = len(CSV_HEADERS[mode]) - 1
    raw_by_variation = _load_raw_groups(csv_path, mode)
    loaded: dict[Variation, LutRow] = {}
    for row in rows[1:valid_row_count]:
        variation = variation_from_csv_row(row, mode)
        if variation is None:
            continue
        try:
            watt = float(row[watt_index])
        except (IndexError, ValueError):
            continue
        loaded[variation] = LutRow(
            variation=variation,
            watt=watt,
            quality=quality_from_raw_lines(
                raw_by_variation.get(variation, ()),
                session_updated_at=session_updated_at,
            ),
        )
    return loaded


def load_session_measured_rows(artifact_directory: Path | str) -> dict[LutMode, dict[Variation, LutRow]]:
    """Return every complete LUT row on disk, keyed by mode."""
    directory = Path(artifact_directory)
    measured: dict[LutMode, dict[Variation, LutRow]] = {}
    for mode in CSV_HEADERS:
        rows = load_mode_rows(directory / f"{mode.value}.csv", mode)
        if rows:
            measured[mode] = rows
    return measured


def load_session_measured_variations(artifact_directory: Path | str) -> dict[LutMode, set[Variation]]:
    """Return every complete LUT key on disk, keyed by mode."""
    return {mode: set(rows) for mode, rows in load_session_measured_rows(artifact_directory).items()}


def quality_from_raw_lines(
    lines: Sequence[Mapping[str, object]],
    *,
    session_updated_at: str | None = None,
) -> LutQuality:
    """Build ranking inputs from the raw lines that belong to one LUT key.

    Old single-line raw files count as one reading. Missing raw data is one unknown
    reading so a CSV-only session can still merge.
    """
    if not lines:
        return LutQuality(
            settle_rank=_SETTLE_UNKNOWN,
            reading_count=1,
            witness_agrees=None,
            timestamp=None,
            session_updated_at=session_updated_at,
        )

    settle_rank = _settle_rank(lines[0].get("settle_hit_cap"))
    timestamps = [float(line["timestamp"]) for line in lines if isinstance(line.get("timestamp"), int | float)]
    witness_votes: list[bool] = []
    for line in lines:
        witnesses = line.get("witnesses")
        if not isinstance(witnesses, list) or not witnesses:
            continue
        witness_votes.append(
            all(isinstance(witness, dict) and witness.get("agrees") is True for witness in witnesses),
        )
    return LutQuality(
        settle_rank=settle_rank,
        reading_count=len(lines),
        witness_agrees=all(witness_votes) if witness_votes else None,
        timestamp=max(timestamps) if timestamps else None,
        session_updated_at=session_updated_at,
    )


def compare_quality(left: LutQuality, right: LutQuality) -> tuple[Literal["left", "right"], ReplaceReason | None]:
    """Return which quality wins and the first differing reason.

    A tie keeps ``left`` (the row already in the union).
    """
    if left.settle_rank != right.settle_rank:
        winner: Literal["left", "right"] = "left" if left.settle_rank > right.settle_rank else "right"
        return winner, ReplaceReason.SETTLE
    if left.reading_count != right.reading_count:
        winner = "left" if left.reading_count > right.reading_count else "right"
        return winner, ReplaceReason.SAMPLES
    if (
        left.witness_agrees is not None
        and right.witness_agrees is not None
        and left.witness_agrees != right.witness_agrees
    ):
        winner = "left" if left.witness_agrees else "right"
        return winner, ReplaceReason.WITNESS
    left_recency = _recency(left)
    right_recency = _recency(right)
    if left_recency != right_recency:
        winner = "left" if left_recency > right_recency else "right"
        return winner, ReplaceReason.NEWER
    return "left", None


def union_rows(*tables: Mapping[Variation, LutRow], prefer_last: bool = False) -> dict[Variation, LutRow]:
    """Union LUT tables by variation key. Never append a duplicate key.

    Default clashes keep the higher-quality row. ``prefer_last`` is the explicit
    remeasure path: a later table always replaces, quality rank does not apply.
    """
    merged: dict[Variation, LutRow] = {}
    for table in tables:
        for key, row in table.items():
            existing = merged.get(key)
            if existing is None or prefer_last:
                merged[key] = row
                continue
            winner, _reason = compare_quality(existing.quality, row.quality)
            if winner == "right":
                merged[key] = row
    return merged


def preview_union(
    left: Mapping[Variation, LutRow],
    right: Mapping[Variation, LutRow],
) -> ModeUnionPreview:
    """Count kept / added / replaced keys and why replacements won."""
    reasons = {reason.value: 0 for reason in ReplaceReason}
    kept = 0
    added = 0
    replaced = 0
    for key in set(left) | set(right):
        left_row = left.get(key)
        right_row = right.get(key)
        if left_row is None:
            added += 1
            continue
        if right_row is None:
            kept += 1
            continue
        winner, reason = compare_quality(left_row.quality, right_row.quality)
        if winner == "right" and reason is not None:
            replaced += 1
            reasons[reason.value] += 1
        else:
            kept += 1
    return ModeUnionPreview(kept=kept, added=added, replaced=replaced, replaced_reasons=reasons)


def write_mode_csv(
    path: Path | str,
    mode: LutMode,
    rows: Mapping[Variation, LutRow],
    *,
    plan: Sequence[Variation] | None = None,
    gzip_output: bool = True,
) -> None:
    """Write a mode CSV (and optionally ``.csv.gz``) in plan order, then leftover keys."""
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = _ordered_rows(rows, plan)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADERS[mode])
        for row in ordered:
            writer.writerow([*row.variation.to_csv_row(), row.watt])
    if gzip_output:
        with csv_path.open("rb") as source, gzip.open(f"{csv_path}.gz", "wb") as compressed:
            shutil.copyfileobj(source, compressed)


def concatenate_raw_jsonl(destination: Path, *sources: Path) -> None:
    """Concatenate diagnostic raw logs. The LUT CSV remains the watt source of truth."""
    chunks: list[str] = []
    for source in sources:
        if not source.is_file() or source.is_symlink():
            continue
        text = source.read_text(encoding="utf-8")
        if not text:
            continue
        chunks.append(text if text.endswith("\n") else f"{text}\n")
    if not chunks:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(chunks), encoding="utf-8")


def _ordered_rows(rows: Mapping[Variation, LutRow], plan: Sequence[Variation] | None) -> list[LutRow]:
    ordered: list[LutRow] = []
    seen: set[Variation] = set()
    for variation in plan or ():
        row = rows.get(variation)
        if row is None:
            continue
        ordered.append(row)
        seen.add(variation)
    leftovers = [row for key, row in rows.items() if key not in seen]
    leftovers.sort(key=lambda row: tuple(str(value) for value in row.variation.to_csv_row()))
    ordered.extend(leftovers)
    return ordered


def _settle_rank(settle_hit_cap: object) -> int:
    if settle_hit_cap is False:
        return _SETTLE_SETTLED
    if settle_hit_cap is True:
        return _SETTLE_TIMED_OUT
    return _SETTLE_UNKNOWN


def _recency(quality: LutQuality) -> tuple[float, str]:
    """Later raw timestamp wins; otherwise later session ``updated_at``."""
    return (quality.timestamp if quality.timestamp is not None else float("-inf"), quality.session_updated_at or "")


def _load_raw_groups(csv_path: Path, mode: LutMode) -> dict[Variation, list[dict[str, object]]]:
    raw_path = csv_path.with_name(f"{csv_path.stem}.raw.jsonl")
    if not raw_path.is_file() or raw_path.is_symlink():
        return {}
    try:
        text = raw_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    grouped: dict[Variation, list[dict[str, object]]] = {}
    pending: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if pending is not None:
            _append_raw_line(grouped, pending, mode)
        pending = line
    if pending is not None:
        with suppress(json.JSONDecodeError):
            _append_raw_line(grouped, pending, mode)
    return grouped


def _append_raw_line(grouped: dict[Variation, list[dict[str, object]]], line: str, mode: LutMode) -> None:
    parsed = json.loads(line)
    if not isinstance(parsed, dict):
        return
    variation = _variation_from_raw(mode, parsed)
    if variation is None:
        return
    grouped.setdefault(variation, []).append(parsed)


def _variation_from_raw(mode: LutMode, row: Mapping[str, object]) -> Variation | None:
    payload = row.get("variation")
    if not isinstance(payload, dict):
        return None
    try:
        if mode == LutMode.BRIGHTNESS:
            return Variation(bri=int(payload["bri"]))
        if mode == LutMode.COLOR_TEMP:
            mired = payload.get("ct", payload.get("mired"))
            return ColorTempVariation(bri=int(payload["bri"]), ct=int(mired))
        if mode == LutMode.HS:
            return HsVariation(bri=int(payload["bri"]), hue=int(payload["hue"]), sat=int(payload["sat"]))
        if mode == LutMode.EFFECT:
            effect = str(payload["effect"]).strip()
            return EffectVariation(effect=effect, bri=int(payload["bri"])) if effect else None
    except (KeyError, TypeError, ValueError):
        return None
    return None
