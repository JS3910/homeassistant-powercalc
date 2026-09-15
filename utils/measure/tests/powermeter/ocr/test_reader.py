from measure.powermeter.ocr.engine import TextBox
from measure.powermeter.ocr.layout import PR10, DisplayLayout
from measure.powermeter.ocr.reader import DisplayReader, rotate
import numpy as np
import pytest

from tests.powermeter.ocr.conftest import (
    PF_BOX,
    POWER_BOX,
    VI_BOX,
    FakeEngine,
    blank_frame,
    rect_of,
    rows,
    scripted_engine,
)


def test_locate_matches_rows_to_fields_without_levelling_a_level_frame() -> None:
    engine = scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354")
    reader = DisplayReader(engine, PR10)

    location = reader.locate(blank_frame())

    assert location is not None
    assert location.angle == 0.0
    assert location.regions == {
        "power": rect_of(POWER_BOX),
        "voltage": rect_of(VI_BOX),
        "current": rect_of(VI_BOX),
        "pf": rect_of(PF_BOX),
    }
    assert location.rects() == [rect_of(POWER_BOX), rect_of(VI_BOX), rect_of(PF_BOX)]
    assert engine.detect_calls == 1
    assert reader.location is location


def test_read_recognises_only_the_located_regions_and_accepts_consistent_values() -> None:
    engine = scripted_engine("Pawer4.60w", "233.0vC0.055A", "PF0.354")
    reader = DisplayReader(engine, PR10)
    reader.locate(blank_frame())

    reading = reader.read(blank_frame(), timestamp=10.0)

    assert reading.accepted
    assert reading.reason is None
    assert (reading.power, reading.voltage, reading.current, reading.pf) == (4.60, 233.0, 0.055, 0.354)
    assert reading.raw == {
        "power": "Pawer4.60w",
        "voltage": "233.0vC0.055A",
        "current": "233.0vC0.055A",
        "pf": "PF0.354",
    }
    assert reading.timestamp == 10.0
    assert engine.recognize_calls == 3  # three distinct rows, voltage and current share one


def test_read_rejects_when_a_field_does_not_parse() -> None:
    engine = scripted_engine("Power4.60w", "V233.1V055A", "PF0.354")
    reader = DisplayReader(engine, PR10)
    reader.locate(blank_frame())

    reading = reader.read(blank_frame(), timestamp=1.0)

    assert not reading.accepted
    assert reading.reason == "current unreadable: 'V233.1V055A'"
    assert reading.power is None
    assert reading.raw["current"] == "V233.1V055A"


def test_read_rejects_a_dropped_decimal_through_the_cross_check() -> None:
    # 406.7 W read as 4067 W: V*I*PF says otherwise.
    engine = scripted_engine("Power4067w", "V230.0vC1.780A", "PF0.994")
    reader = DisplayReader(engine, PR10)
    reader.locate(blank_frame())

    reading = reader.read(blank_frame(), timestamp=1.0)

    assert not reading.accepted
    assert reading.reason is not None
    assert reading.reason == "power 4067 W disagrees with V x I x PF = 230 x 1.78 x 0.994 = 406.94 W (90.0% > 3%)"
    assert reading.power == 4067.0  # still reported, for the preview


def test_read_rejects_a_reading_above_the_plausible_power_bound_even_when_internally_consistent() -> None:
    # A misread that shifts every field by the same wrong factor (a missed decimal point
    # across the whole display, say) can stay internally consistent and still pass the
    # V*I*PF crosscheck -- only an absolute bound catches it.
    engine = scripted_engine("Power406.7w", "V230.0vC1.780A", "PF0.994")
    reader = DisplayReader(engine, PR10, max_plausible_power_w=100.0)
    reader.locate(blank_frame())

    reading = reader.read(blank_frame(), timestamp=1.0)

    assert not reading.accepted
    assert reading.reason == "power 406.7 W exceeds the plausible bound of 100 W"
    assert reading.power == 406.7  # still reported, for the preview


def test_read_accepts_a_reading_within_the_plausible_power_bound() -> None:
    engine = scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354")
    reader = DisplayReader(engine, PR10, max_plausible_power_w=100.0)
    reader.locate(blank_frame())

    assert reader.read(blank_frame(), timestamp=1.0).accepted


def test_read_applies_no_bound_when_none_is_configured() -> None:
    engine = scripted_engine("Power4067w", "V230.0vC1.780A", "PF0.994")
    reader = DisplayReader(engine, PR10)  # crosscheck alone still rejects this one
    reader.locate(blank_frame())

    reading = reader.read(blank_frame(), timestamp=1.0)

    assert not reading.accepted
    assert "exceeds the plausible bound" not in (reading.reason or "")


def test_cross_check_is_skipped_below_the_current_floor_and_respects_tolerance() -> None:
    # PF and current are unreliable near zero; a 1 W bulb reads 0.000 A.
    engine = scripted_engine("Power1.00w", "V233.4vC0.000A", "PF0.000")
    reader = DisplayReader(engine, PR10)
    reader.locate(blank_frame())
    assert reader.read(blank_frame(), timestamp=1.0).accepted

    engine = scripted_engine("Power4.70w", "V233.0vC0.055A", "PF0.354")  # 3.6% off
    reader = DisplayReader(engine, PR10, crosscheck_tolerance_pct=3.0)
    reader.locate(blank_frame())
    assert not reader.read(blank_frame(), timestamp=1.0).accepted
    reader = DisplayReader(engine, PR10, crosscheck_tolerance_pct=5.0)
    reader.locate(blank_frame())
    assert reader.read(blank_frame(), timestamp=1.0).accepted

    # Everything at zero (a plugged-in meter with nothing behind it) is consistent, not a misread.
    engine = scripted_engine("Power0.00w", "V000.0vC0.050A", "PF0.000")
    reader = DisplayReader(engine, PR10)
    reader.locate(blank_frame())
    assert reader.read(blank_frame(), timestamp=1.0).accepted


def test_layout_without_the_cross_check_fields_is_accepted_on_parsing_alone() -> None:
    power_only = DisplayLayout(name="power-only", fields={"power": PR10.fields["power"]}, row_order=(("power",),))
    engine = scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354")
    reader = DisplayReader(engine, power_only)
    assert reader.locate(blank_frame()) is not None
    reading = reader.read(blank_frame(), timestamp=1.0)
    assert reading.accepted
    assert reading.power == 4.6
    assert reading.voltage is None


def test_read_without_location_is_rejected() -> None:
    reader = DisplayReader(FakeEngine(), PR10)
    reading = reader.read(blank_frame(), timestamp=1.0)
    assert not reading.accepted
    assert reading.reason == "display not located"


def test_locate_fails_when_nothing_is_detected_or_rows_are_missing() -> None:
    reader = DisplayReader(FakeEngine(detections=[]), PR10)
    assert reader.locate(blank_frame()) is None

    engine = FakeEngine(detections=[[TextBox(POWER_BOX, "Power4.60w"), TextBox(PF_BOX, "PF0.354")]])
    reader = DisplayReader(engine, PR10)
    assert reader.locate(blank_frame()) is None
    assert reader.location is None


def test_locate_prefers_the_row_whose_value_parses_over_a_label_only_row() -> None:
    label_only = ((325.0, 250.0), (371.0, 253.0), (370.0, 276.0), (323.0, 273.0))
    engine = FakeEngine(
        detections=[
            [
                TextBox(POWER_BOX, "Power4.60w"),
                TextBox(label_only, "Veg"),
                TextBox(VI_BOX, "233.0v C0.055A"),
                TextBox(PF_BOX, "PF0.354"),
            ],
        ],
    )
    location = DisplayReader(engine, PR10).locate(blank_frame())
    assert location is not None
    assert location.regions["voltage"] == rect_of(VI_BOX)
    assert location.regions["current"] == rect_of(VI_BOX)


def test_locate_shares_a_row_with_a_sibling_whose_label_was_not_read() -> None:
    engine = FakeEngine(detections=[rows("Power4.60w", "V233.0v", "PF0.354")])
    location = DisplayReader(engine, PR10).locate(blank_frame())
    assert location is not None
    assert location.regions["current"] == location.regions["voltage"] == rect_of(VI_BOX)


def test_locate_rejects_rows_out_of_order() -> None:
    engine = FakeEngine(
        detections=[[TextBox(PF_BOX, "Power4.60w"), TextBox(VI_BOX, "V233.0vC0.055A"), TextBox(POWER_BOX, "PF0.354")]],
    )
    assert DisplayReader(engine, PR10).locate(blank_frame()) is None


def test_locate_levels_a_tilted_display_and_tries_the_ambiguous_orientations() -> None:
    def tilted(points: tuple[tuple[float, float], ...], degrees: float) -> tuple[tuple[float, float], ...]:
        angle = np.radians(degrees)
        rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        return tuple((float(x), float(y)) for x, y in (np.asarray(points) - 400) @ rotation.T + 400)

    # Upside-down text, 3 degrees off: every box's baseline reads as 3 degrees, and the
    # rows come out bottom-to-top, so the raw frame fails the row-order check. The reader
    # then re-detects on the frame rotated by the tilt; the fake returns level rows for
    # that second call.
    level = [
        TextBox(((320.0, 190.0), (490.0, 190.0), (490.0, 240.0), (320.0, 240.0)), "Power1178w"),
        TextBox(((320.0, 250.0), (540.0, 250.0), (540.0, 290.0), (320.0, 290.0)), "V228.1vC5.191A"),
        TextBox(((320.0, 300.0), (400.0, 300.0), (400.0, 330.0), (320.0, 330.0)), "PF0.995"),
    ]
    upside_down = [TextBox(tilted(box.points, 180 + 3), box.text) for box in level]
    engine = FakeEngine(detections=[upside_down, level])
    reader = DisplayReader(engine, PR10)

    location = reader.locate(blank_frame())

    assert location is not None
    assert location.angle == pytest.approx(3.0, abs=0.01)
    assert engine.detect_calls == 2


def test_locate_gives_up_after_all_orientations_fail() -> None:
    skewed = [TextBox(((0.0, 0.0), (10.0, 10.0), (9.0, 11.0), (-1.0, 1.0)), "Power4.60w")]
    engine = FakeEngine(detections=[skewed, [], [], [], []])
    reader = DisplayReader(engine, PR10)
    assert reader.locate(blank_frame()) is None
    assert engine.detect_calls == 5  # raw frame plus the four levelled candidates


def test_rotate_enlarges_the_canvas_and_skips_tiny_angles() -> None:
    frame = blank_frame(600, 800)
    assert rotate(frame, 0.5) is frame
    rotated = rotate(frame, 90)
    assert rotated.shape[:2] == (800, 600)
    assert rotate(frame, 45).shape[0] > 600


def test_invalidate_forgets_the_location() -> None:
    reader = DisplayReader(scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354"), PR10)
    reader.locate(blank_frame())
    reader.invalidate()
    assert reader.location is None


def test_rows_must_line_up_in_one_column() -> None:
    aside = ((900.0, 280.0), (950.0, 280.0), (950.0, 310.0), (900.0, 310.0))
    engine = FakeEngine(
        detections=[[TextBox(POWER_BOX, "Power4.60w"), TextBox(VI_BOX, "V233.0vC0.055A"), TextBox(aside, "PF0.354")]]
    )
    assert DisplayReader(engine, PR10).locate(blank_frame()) is None


def test_crop_outside_the_frame_reads_as_empty() -> None:
    below_frame = ((330.0, 700.0), (400.0, 700.0), (400.0, 720.0), (330.0, 720.0))
    engine = FakeEngine(
        detections=[
            [TextBox(POWER_BOX, "Power4.60w"), TextBox(VI_BOX, "V233.0vC0.055A"), TextBox(below_frame, "PF0.354")]
        ],
    )
    reader = DisplayReader(engine, PR10)
    assert reader.locate(blank_frame()) is not None
    reading = reader.read(blank_frame(), timestamp=1.0)
    assert reading.reason == "power unreadable: ''"


def test_box_angle_from_longer_edge() -> None:
    assert TextBox(((0.0, 0.0), (100.0, 0.0), (100.0, 20.0), (0.0, 20.0)), "x").angle == 0.0
    assert TextBox(((0.0, 0.0), (0.0, 100.0), (-20.0, 100.0), (-20.0, 0.0)), "x").angle == -90.0
    assert TextBox(((0.0, 0.0), (100.0, 100.0), (90.0, 110.0), (-10.0, 10.0)), "x").angle == pytest.approx(45.0)
    assert TextBox(((100.0, 0.0), (0.0, 0.0), (0.0, 20.0), (100.0, 20.0)), "x").angle == pytest.approx(0.0)
