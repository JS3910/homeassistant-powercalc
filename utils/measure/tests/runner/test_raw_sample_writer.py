import json
from pathlib import Path

from measure.controller.light.const import LutMode
from measure.powermeter.composite import CompositeReading, WitnessReading
from measure.powermeter.powermeter import PowerMeasurementResult
from measure.runner.light_plan import Variation
from measure.runner.raw_sample_writer import RawSampleWriter


def test_write_appends_one_json_line_with_primary_and_witness_fields(tmp_path: Path) -> None:
    path = tmp_path / "brightness.raw.jsonl"
    writer = RawSampleWriter(str(path))
    reading = CompositeReading(
        primary=PowerMeasurementResult(power=2.8, updated=1.0, voltage=230.0, current=0.012, power_factor=1.0),
        witnesses=(
            WitnessReading(
                name="ocr",
                power=0.35,
                corrected=0.35,
                deviation=0.0,
                agrees=True,
                voltage=230.1,
                current=0.0015,
                power_factor=0.99,
            ),
        ),
    )

    writer.write(mode=LutMode.BRIGHTNESS, variation=Variation(128), reading=reading)
    writer.close()

    lines = path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["mode"] == "brightness"
    assert row["variation"] == {"bri": 128}
    assert row["primary"] == {"power": 2.8, "voltage": 230.0, "current": 0.012, "power_factor": 1.0}
    assert row["witnesses"] == [
        {
            "name": "ocr",
            "power": 0.35,
            "corrected": 0.35,
            "deviation": 0.0,
            "agrees": True,
            "error": None,
            "voltage": 230.1,
            "current": 0.0015,
            "power_factor": 0.99,
        },
    ]


def test_write_is_a_noop_when_there_is_no_composite_reading(tmp_path: Path) -> None:
    path = tmp_path / "brightness.raw.jsonl"
    writer = RawSampleWriter(str(path))

    writer.write(mode=LutMode.BRIGHTNESS, variation=Variation(1), reading=None)
    writer.close()

    assert path.read_text() == ""


def test_write_appends_multiple_lines_across_calls(tmp_path: Path) -> None:
    path = tmp_path / "brightness.raw.jsonl"
    writer = RawSampleWriter(str(path))
    reading = CompositeReading(
        primary=PowerMeasurementResult(power=1.0, updated=1.0),
        witnesses=(
            WitnessReading(name="shelly", power=None, corrected=None, deviation=None, agrees=False, error="timeout"),
        ),
    )

    writer.write(mode=LutMode.BRIGHTNESS, variation=Variation(1), reading=reading)
    writer.write(mode=LutMode.BRIGHTNESS, variation=Variation(2), reading=reading)
    writer.close()

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["variation"] == {"bri": 1}
    assert json.loads(lines[1])["variation"] == {"bri": 2}
    assert json.loads(lines[1])["witnesses"][0]["error"] == "timeout"


def test_write_records_settle_seconds_and_whether_it_hit_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "brightness.raw.jsonl"
    writer = RawSampleWriter(str(path))
    reading = CompositeReading(primary=PowerMeasurementResult(power=1.0, updated=1.0), witnesses=())

    writer.write(
        mode=LutMode.BRIGHTNESS, variation=Variation(1), reading=reading, settle_seconds=0.8, settle_hit_cap=False
    )
    writer.write(
        mode=LutMode.BRIGHTNESS, variation=Variation(2), reading=reading, settle_seconds=3.0, settle_hit_cap=True
    )
    writer.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["settle_seconds"] == 0.8
    assert rows[0]["settle_hit_cap"] is False
    assert rows[1]["settle_seconds"] == 3.0
    assert rows[1]["settle_hit_cap"] is True


def test_write_records_settle_data_even_with_no_composite_reading(tmp_path: Path) -> None:
    """Settle detection works with any power meter, not just composite ones."""
    path = tmp_path / "brightness.raw.jsonl"
    writer = RawSampleWriter(str(path))

    writer.write(
        mode=LutMode.BRIGHTNESS, variation=Variation(1), reading=None, settle_seconds=1.2, settle_hit_cap=False
    )
    writer.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["settle_seconds"] == 1.2
    assert "primary" not in rows[0]


def test_write_is_still_a_noop_with_neither_reading_nor_settle_data(tmp_path: Path) -> None:
    path = tmp_path / "brightness.raw.jsonl"
    writer = RawSampleWriter(str(path))

    writer.write(mode=LutMode.BRIGHTNESS, variation=Variation(1), reading=None)
    writer.close()

    assert path.read_text() == ""


def test_close_does_not_raise_when_called_twice(tmp_path: Path) -> None:
    path = tmp_path / "brightness.raw.jsonl"
    writer = RawSampleWriter(str(path))
    writer.close()
    writer.close()
