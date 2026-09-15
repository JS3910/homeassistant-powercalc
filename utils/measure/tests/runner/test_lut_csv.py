import json
from pathlib import Path

from measure.controller.light.const import LutMode
from measure.runner.light_plan import ColorTempVariation, Variation
from measure.runner.lut_csv import (
    LutQuality,
    LutRow,
    ReplaceReason,
    compare_quality,
    load_mode_rows,
    missing_variations,
    preview_union,
    union_rows,
    write_mode_csv,
)


def _row(
    variation: Variation,
    watt: float,
    *,
    settle_rank: int = 1,
    reading_count: int = 1,
    witness_agrees: bool | None = None,
    timestamp: float | None = None,
    session_updated_at: str | None = None,
) -> LutRow:
    return LutRow(
        variation=variation,
        watt=watt,
        quality=LutQuality(
            settle_rank=settle_rank,
            reading_count=reading_count,
            witness_agrees=witness_agrees,
            timestamp=timestamp,
            session_updated_at=session_updated_at,
        ),
    )


def test_load_mode_rows_drops_a_torn_trailing_line(tmp_path: Path) -> None:
    path = tmp_path / "brightness.csv"
    path.write_text("bri,watt\n1,1.0\n2,2.0\n3,", encoding="utf-8")

    rows = load_mode_rows(path, LutMode.BRIGHTNESS)

    assert set(rows) == {Variation(1), Variation(2)}
    assert rows[Variation(2)].watt == 2.0


def test_load_mode_rows_joins_raw_reading_count_and_settle(tmp_path: Path) -> None:
    csv_path = tmp_path / "brightness.csv"
    csv_path.write_text("bri,watt\n1,1.5\n", encoding="utf-8")
    (tmp_path / "brightness.raw.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": 10.0,
                        "mode": "brightness",
                        "variation": {"bri": 1},
                        "settle_seconds": 0.8,
                        "settle_hit_cap": False,
                        "primary": {"power": 1.4},
                    },
                ),
                json.dumps(
                    {
                        "timestamp": 11.0,
                        "mode": "brightness",
                        "variation": {"bri": 1},
                        "settle_seconds": 0.8,
                        "settle_hit_cap": False,
                        "primary": {"power": 1.6},
                    },
                ),
            ],
        )
        + "\n",
        encoding="utf-8",
    )

    rows = load_mode_rows(csv_path, LutMode.BRIGHTNESS)

    quality = rows[Variation(1)].quality
    assert quality.reading_count == 2
    assert quality.settle_rank == 2
    assert quality.timestamp == 11.0


def test_load_mode_rows_counts_a_csv_only_key_as_one_unknown_reading(tmp_path: Path) -> None:
    path = tmp_path / "brightness.csv"
    path.write_text("bri,watt\n8,3.0\n", encoding="utf-8")

    rows = load_mode_rows(path, LutMode.BRIGHTNESS)

    assert rows[Variation(8)].quality.reading_count == 1
    assert rows[Variation(8)].quality.settle_rank == 1


def test_union_rows_never_keeps_a_duplicate_key() -> None:
    left = {Variation(1): _row(Variation(1), 1.0, settle_rank=0, reading_count=8)}
    right = {Variation(1): _row(Variation(1), 2.0, settle_rank=2, reading_count=1)}

    merged = union_rows(left, right)

    assert list(merged) == [Variation(1)]
    assert merged[Variation(1)].watt == 2.0


def test_settled_one_reading_beats_timed_out_eight_readings() -> None:
    winner, reason = compare_quality(
        LutQuality(settle_rank=2, reading_count=1, witness_agrees=None, timestamp=1.0),
        LutQuality(settle_rank=0, reading_count=8, witness_agrees=None, timestamp=9.0),
    )

    assert winner == "left"
    assert reason == ReplaceReason.SETTLE


def test_reading_count_breaks_a_settle_tie() -> None:
    winner, reason = compare_quality(
        LutQuality(settle_rank=2, reading_count=1, witness_agrees=None, timestamp=1.0),
        LutQuality(settle_rank=2, reading_count=3, witness_agrees=None, timestamp=1.0),
    )

    assert winner == "right"
    assert reason == ReplaceReason.SAMPLES


def test_witness_is_skipped_unless_both_sides_have_it() -> None:
    winner, reason = compare_quality(
        LutQuality(settle_rank=1, reading_count=1, witness_agrees=True, timestamp=1.0),
        LutQuality(settle_rank=1, reading_count=1, witness_agrees=None, timestamp=9.0),
    )

    assert winner == "right"
    assert reason == ReplaceReason.NEWER


def test_prefer_last_replaces_without_quality_rank() -> None:
    seed = {Variation(1): _row(Variation(1), 1.0, settle_rank=2, reading_count=8)}
    newly = {Variation(1): _row(Variation(1), 9.0, settle_rank=0, reading_count=1)}

    merged = union_rows(seed, newly, prefer_last=True)

    assert merged[Variation(1)].watt == 9.0


def test_preview_union_reports_why_a_key_was_replaced() -> None:
    left = {
        Variation(1): _row(Variation(1), 1.0, settle_rank=0),
        Variation(2): _row(Variation(2), 2.0, settle_rank=2),
    }
    right = {
        Variation(1): _row(Variation(1), 1.5, settle_rank=2),
        Variation(3): _row(Variation(3), 3.0),
    }

    preview = preview_union(left, right)

    assert preview.kept == 1
    assert preview.added == 1
    assert preview.replaced == 1
    assert preview.replaced_reasons["settle"] == 1


def test_write_mode_csv_uses_plan_order_then_leftover_keys(tmp_path: Path) -> None:
    plan = [ColorTempVariation(1, 200), ColorTempVariation(2, 200)]
    leftover = ColorTempVariation(1, 553)
    rows = {
        leftover: _row(leftover, 0.3),
        plan[1]: _row(plan[1], 2.0),
        plan[0]: _row(plan[0], 1.0),
    }
    path = tmp_path / "color_temp.csv"

    write_mode_csv(path, LutMode.COLOR_TEMP, rows, plan=plan)

    assert path.read_text(encoding="utf-8").splitlines() == [
        "bri,mired,watt",
        "1,200,1.0",
        "2,200,2.0",
        "1,553,0.3",
    ]
    assert path.with_suffix(".csv.gz").is_file()


def test_missing_variations_preserves_plan_order() -> None:
    plan = [Variation(1), Variation(2), Variation(3)]

    assert missing_variations(plan, {Variation(2): _row(Variation(2), 2.0)}) == [Variation(1), Variation(3)]
