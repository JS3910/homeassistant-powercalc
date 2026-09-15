import gzip
from pathlib import Path

from measure.controller.light.const import LutMode
from measure.request import MeasurementRequest, parse_measurement_request
from measure.runner.light_plan import ColorTempVariation, HsVariation, Variation
from measure.runner.lut_csv import LutQuality, LutRow, write_mode_csv
from measure.runner.plot_edits import (
    PlotEditError,
    apply_plot_action,
    filter_profile_csv_bytes,
    load_ignored,
)
from measure.visualization import build_session_plots
import pytest


def light_request(*modes: str) -> MeasurementRequest:
    return parse_measurement_request(
        {
            "measure_type": "light",
            "model_id": "LCT010",
            "product_name": "Test light",
            "measure_device": "Test meter",
            "power_meter": {"type": "dummy"},
            "controller": {"type": "dummy"},
            "modes": list(modes),
        },
    )


def _row(variation: Variation, watt: float) -> LutRow:
    return LutRow(
        variation=variation,
        watt=watt,
        quality=LutQuality(settle_rank=1, reading_count=1, witness_agrees=None, timestamp=None),
    )


def _session_csv(tmp_path: Path, mode: LutMode, rows: dict) -> Path:
    path = tmp_path / "output" / "LCT010" / f"{mode.value}.csv"
    write_mode_csv(path, mode, rows, gzip_output=False)
    return path


def test_ignore_is_sidecar_only(tmp_path: Path) -> None:
    csv_path = _session_csv(
        tmp_path,
        LutMode.BRIGHTNESS,
        {Variation(1): _row(Variation(1), 0.5), Variation(255): _row(Variation(255), 8.2)},
    )
    apply_plot_action(tmp_path, light_request("brightness"), "brightness:255", "ignore")

    assert load_ignored(tmp_path) == {"brightness:255"}
    assert "8.2" in csv_path.read_text(encoding="utf-8")


def test_edit_and_delete_rewrite_the_csv(tmp_path: Path) -> None:
    csv_path = _session_csv(
        tmp_path,
        LutMode.BRIGHTNESS,
        {Variation(1): _row(Variation(1), 0.5), Variation(255): _row(Variation(255), 8.2)},
    )
    apply_plot_action(tmp_path, light_request("brightness"), "brightness:255", "edit", watt=7.5)
    assert "7.5" in csv_path.read_text(encoding="utf-8")

    apply_plot_action(tmp_path, light_request("brightness"), "brightness:1", "delete")
    text = csv_path.read_text(encoding="utf-8")
    assert "0.5" not in text
    assert "7.5" in text


def test_fix_outlier_averages_rail_neighbors(tmp_path: Path) -> None:
    def red(bri: int, watt: float) -> tuple[HsVariation, LutRow]:
        variation = HsVariation(bri=bri, hue=1, sat=255)
        return variation, _row(variation, watt)

    rows = dict((red(1, 1.0), red(128, 9.0), red(255, 3.0)))
    _session_csv(tmp_path, LutMode.HS, rows)

    apply_plot_action(tmp_path, light_request("hs"), "hs:128:1:255", "fix_outlier")
    result = build_session_plots(light_request("hs"), {"LCT010/hs.csv": tmp_path / "output" / "LCT010" / "hs.csv"})
    middle = next(point for point in result.plots[0].series[0].points if point.id == "hs:128:1:255")
    assert middle.y == pytest.approx(2.0)


def test_copied_mode_can_be_edited_when_not_in_this_request(tmp_path: Path) -> None:
    """An HS-only refine still has the seed CT CSV; result-page edits must reach it."""

    warm = ColorTempVariation(bri=255, ct=150)
    mid = ColorTempVariation(bri=255, ct=204)
    cold = ColorTempVariation(bri=255, ct=500)
    _session_csv(
        tmp_path,
        LutMode.COLOR_TEMP,
        {warm: _row(warm, 4.6), mid: _row(mid, 0.16), cold: _row(cold, 2.5)},
    )

    apply_plot_action(tmp_path, light_request("hs"), "color_temp:255:204", "edit", watt=3.55)

    text = (tmp_path / "output" / "LCT010" / "color_temp.csv").read_text(encoding="utf-8")
    assert "3.55" in text


def test_fix_outlier_needs_both_neighbors(tmp_path: Path) -> None:
    _session_csv(
        tmp_path,
        LutMode.COLOR_TEMP,
        {
            ColorTempVariation(bri=1, ct=150): _row(ColorTempVariation(bri=1, ct=150), 0.4),
            ColorTempVariation(bri=255, ct=150): _row(ColorTempVariation(bri=255, ct=150), 4.6),
        },
    )
    with pytest.raises(PlotEditError, match="brightness-rail"):
        apply_plot_action(tmp_path, light_request("color_temp"), "color_temp:255:150", "fix_outlier")


def test_fix_outlier_stays_on_the_brightness_rail_at_100_percent(tmp_path: Path) -> None:
    """A 100% point must not be averaged against other color temps at the same bri."""

    rail = 150
    other = 400
    _session_csv(
        tmp_path,
        LutMode.COLOR_TEMP,
        {
            ColorTempVariation(bri=64, ct=rail): _row(ColorTempVariation(bri=64, ct=rail), 1.0),
            ColorTempVariation(bri=128, ct=rail): _row(ColorTempVariation(bri=128, ct=rail), 2.0),
            ColorTempVariation(bri=255, ct=rail): _row(ColorTempVariation(bri=255, ct=rail), 9.0),
            ColorTempVariation(bri=255, ct=other): _row(ColorTempVariation(bri=255, ct=other), 4.6),
        },
    )

    apply_plot_action(tmp_path, light_request("color_temp"), "color_temp:255:150", "fix_outlier")

    result = build_session_plots(
        light_request("color_temp"),
        {"LCT010/color_temp.csv": tmp_path / "output" / "LCT010" / "color_temp.csv"},
    )
    fixed = next(point for point in result.plots[0].series[0].points if point.id == "color_temp:255:150")
    # Linear extrapolation along bri 64→128, not (2.0+4.6)/2 against the other CT.
    assert fixed.y == pytest.approx(3.984375)
    other_point = next(point for point in result.plots[0].series[0].points if point.id == "color_temp:255:400")
    assert other_point.y == pytest.approx(4.6)


def test_profile_export_drops_ignored_rows() -> None:
    raw = b"bri,watt\n1,0.5\n255,8.2\n"
    filtered = filter_profile_csv_bytes(raw, "brightness.csv", {"brightness:255"})
    assert filtered == b"bri,watt\n1,0.5\n"

    gzipped = gzip.compress(raw, mtime=0)
    out = filter_profile_csv_bytes(gzipped, "brightness.csv.gz", {"brightness:1"})
    assert gzip.decompress(out) == b"bri,watt\n255,8.2\n"


def test_plots_mark_ignored_ids(tmp_path: Path) -> None:
    source = tmp_path / "brightness.csv"
    source.write_text("bri,watt\n1,0.5\n255,8.2\n", encoding="utf-8")
    result = build_session_plots(
        light_request("brightness"),
        {"LCT010/brightness.csv": source},
        ignored_ids={"brightness:255"},
    )
    points = result.plots[0].series[0].points
    assert points[0].id == "brightness:1"
    assert points[0].ignored is False
    assert points[1].ignored is True
    assert points[1].stats[0].label == "Power"
    assert points[1].stats[0].value == "8.200 W"
