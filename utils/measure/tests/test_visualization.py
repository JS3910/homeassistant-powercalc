from dataclasses import replace
import gzip
import json
from pathlib import Path
from unittest.mock import patch

from measure.controller.light.const import LutMode
from measure.request import MeasurementRequest, parse_measurement_request
from measure.visualization import (
    PlotDataError,
    PlotKind,
    build_plot_from_file,
    build_session_plots,
    model_has_linear_calibration,
)
from measure.visualization.core import _light_point
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


def test_plots_work_before_model_id_is_known(tmp_path: Path) -> None:
    source = tmp_path / "brightness.csv"
    source.write_text("bri,watt\n1,0.5\n255,8.2\n", encoding="utf-8")
    result = build_session_plots(
        request=light_request("brightness").model_copy(update={"model_id": ""}),
        files={"measurement/brightness.csv": source},
    )
    assert result.plots[0].source == "measurement/brightness.csv"


def test_finished_plots_include_modes_copied_but_not_in_this_request(tmp_path: Path) -> None:
    """An HS-only refine still has the seed CT CSV; the result screen must plot it."""

    files = {
        "LCT010/color_temp.csv": tmp_path / "color_temp.csv",
        "LCT010/hs.csv": tmp_path / "hs.csv",
    }
    files["LCT010/color_temp.csv"].write_text("bri,mired,watt\n255,150,4.6\n255,500,2.5\n", encoding="utf-8")
    files["LCT010/hs.csv"].write_text("bri,hue,sat,watt\n255,1,255,1.3\n", encoding="utf-8")

    result = build_session_plots(light_request("hs"), files)
    live = build_session_plots(light_request("hs"), files, measured_modes_only=True)

    assert {plot.id for plot in result.plots} >= {"color_temp", "color_temp_max_bri", "hs"}
    assert {plot.id for plot in live.plots} == {"hs"}


def test_builds_all_light_plot_modes_from_plain_and_gzip_csv(tmp_path: Path) -> None:
    files = {
        "LCT010/brightness.csv": tmp_path / "brightness.csv",
        "LCT010/color_temp.csv.gz": tmp_path / "color_temp.csv.gz",
        "LCT010/hs.csv": tmp_path / "hs.csv",
        "LCT010/effect.csv": tmp_path / "effect.csv",
    }
    files["LCT010/brightness.csv"].write_text("bri,watt\n1,0.5\n255,8.2\n", encoding="utf-8")
    with gzip.open(files["LCT010/color_temp.csv.gz"], "wt", encoding="utf-8") as file:
        file.write("bri,mired,watt\n1,150,0.6\n255,500,8.5\n")
    files["LCT010/hs.csv"].write_text("bri,hue,sat,watt\n128,32768,255,4.2\n", encoding="utf-8")
    files["LCT010/effect.csv"].write_text(
        "effect,bri,watt\nColor loop,1,0.7\nColor loop,255,9.1\nPulse,128,5.0\n",
        encoding="utf-8",
    )

    result = build_session_plots(
        light_request("brightness", "color_temp", "hs", "effect"),
        files,
    )

    assert result.warnings == ()
    assert [plot.id for plot in result.plots] == [
        "brightness",
        "color_temp",
        "color_temp_max_bri",
        "hs",
        "effect",
    ]
    assert result.plots[0].kind is PlotKind.SCATTER
    assert result.plots[0].series[0].points[-1].y == pytest.approx(8.2)
    assert result.plots[1].series[0].points[0].color is not None
    assert result.plots[3].series[0].points[0].color is not None
    assert [series.label for series in result.plots[4].series] == ["Color loop", "Pulse"]


def test_hue_at_100_percent_includes_intermediate_saturations(tmp_path: Path) -> None:
    """The 100% HS rail plot used to keep only sat-min and sat-max, hiding watt-gap fills."""

    source = tmp_path / "hs.csv"
    source.write_text(
        "bri,hue,sat,watt\n"
        "128,1,255,2.0\n"
        "255,1,255,1.3\n"
        "255,21845,255,1.2\n"
        "255,43690,255,1.2\n"
        "255,1,128,2.8\n"
        "255,21845,64,3.1\n"
        "255,1,1,4.2\n",
        encoding="utf-8",
    )
    result = build_session_plots(light_request("hs"), {"LCT010/hs.csv": source})
    rail = next(plot for plot in result.plots if plot.id == "hs_max_bri")
    assert [series.label for series in rail.series] == [None, None, None]
    assert len(rail.series[0].points) == 3
    assert len(rail.series[1].points) == 2
    assert len(rail.series[2].points) == 1
    brightness_ids = {point.id for series in result.plots[0].series for point in series.points}
    rail_ids = {point.id for series in rail.series for point in series.points}
    assert rail_ids <= brightness_ids
    assert "hs:255:1:255" in rail_ids
    assert next(point for point in rail.series[0].points if point.id == "hs:255:1:255").stats[0].label == "Power"


def test_hs_cylinder_puts_power_on_z_and_near_white_on_the_axis(tmp_path: Path) -> None:
    """Near-white samples share a radius of ~0, so they stack on the axis instead of faking a hue line."""

    source = tmp_path / "hs.csv"
    source.write_text(
        "bri,hue,sat,watt\n"
        "255,1,255,1.3\n"
        "255,21845,255,1.2\n"
        "255,1,1,4.2\n"
        "255,21845,1,4.1\n",
        encoding="utf-8",
    )
    result = build_session_plots(light_request("hs"), {"LCT010/hs.csv": source})
    assert [plot.id for plot in result.plots] == ["hs", "hs_max_bri", "hs_cylinder"]
    cylinder = result.plots[2]
    assert cylinder.kind is PlotKind.CYLINDER
    by_id = {point.id: point for point in cylinder.series[0].points}
    vivid = by_id["hs:255:1:255"]
    white = by_id["hs:255:1:1"]
    other_white = by_id["hs:255:21845:1"]
    assert vivid.x == pytest.approx(1 / 65535 * 360)
    assert vivid.y == 255
    assert vivid.z == pytest.approx(1.3)
    assert white.y == 1
    assert white.z == pytest.approx(4.2)
    assert other_white.y == 1
    assert white.stats[0].label == "Power"
    assert white.stats[0].value == "4.200 W"
    assert "Saturation" in {stat.label for stat in white.stats}


def test_hs_cylinder_includes_every_brightness_slice(tmp_path: Path) -> None:
    source = tmp_path / "hs.csv"
    source.write_text(
        "bri,hue,sat,watt\n"
        "128,1,255,0.8\n"
        "255,1,255,1.3\n"
        "255,21845,255,1.2\n",
        encoding="utf-8",
    )
    result = build_session_plots(light_request("hs"), {"LCT010/hs.csv": source})
    cylinder = next(plot for plot in result.plots if plot.id == "hs_cylinder")
    ids = {point.id for point in cylinder.series[0].points}
    assert ids == {"hs:128:1:255", "hs:255:1:255", "hs:255:21845:255"}
    dim = next(point for point in cylinder.series[0].points if point.id == "hs:128:1:255")
    assert dim.z == pytest.approx(0.8)
    assert dim.interest is None


def test_hs_tooltip_keeps_native_sat_so_nearby_percents_stay_distinct() -> None:
    """Integer percent rounding made sat 4 and sat 5 both read as 2% on 36871."""

    from measure.runner.light_plan import HsVariation
    from measure.visualization.point_meta import point_stats

    left = {stat.label: stat.value for stat in point_stats(LutMode.HS, HsVariation(bri=255, hue=21845, sat=4), 4.47)}
    right = {stat.label: stat.value for stat in point_stats(LutMode.HS, HsVariation(bri=255, hue=21845, sat=5), 2.6)}
    assert left["Saturation"] == "1.6% (4)"
    assert right["Saturation"] == "2.0% (5)"
    assert left["Hue"] == "120.0° (21845)"
    assert left["Brightness"] == "100% (255)"


def test_builds_a_100_percent_ct_rail_plot(tmp_path: Path) -> None:
    files = {
        "LCT010/color_temp.csv": tmp_path / "color_temp.csv",
    }
    files["LCT010/color_temp.csv"].write_text(
        "bri,mired,watt\n1,150,0.4\n255,150,4.6\n255,350,4.9\n255,500,2.5\n",
        encoding="utf-8",
    )
    result = build_session_plots(light_request("color_temp"), files)
    ids = [plot.id for plot in result.plots]
    assert ids == ["color_temp", "color_temp_max_bri"]
    rail = result.plots[1]
    assert rail.x_label == "Color temp (K)"
    assert {point.id for point in rail.series[0].points} == {
        "color_temp:1:150",
        "color_temp:255:150",
        "color_temp:255:350",
        "color_temp:255:500",
    }
    assert [
        round(point.x) for point in rail.series[0].points if point.id and point.id.startswith("color_temp:255:")
    ] == [
        2000,
        2857,
        6667,
    ]


def test_ct_100_percent_rail_marks_interest_points_without_smart_sampling(tmp_path: Path) -> None:
    source = tmp_path / "color_temp.csv"
    source.write_text(
        "bri,mired,watt\n"
        + "\n".join(
            f"255,{ct},{watt}"
            for ct, watt in (
                (153, 4.45),
                (180, 4.48),
                (204, 0.20),
                (220, 4.47),
                (250, 4.35),
                (280, 4.50),
                (320, 4.65),
                (370, 4.80),
                (400, 4.60),
                (410, 3.68),
                (420, 3.70),
                (480, 3.20),
                (555, 2.40),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    request = light_request("color_temp")

    rail = next(
        plot
        for plot in build_session_plots(request, {"LCT010/color_temp.csv": source}).plots
        if plot.id == "color_temp_max_bri"
    )

    marked = {int(round(1_000_000 / mark.x)): mark.label for mark in rail.markers}
    assert marked[400].startswith("discontinuity at ")
    assert marked[410].startswith("discontinuity at ")
    assert marked[370].startswith("segment maximum at ")
    assert marked[250].startswith("segment minimum at ")
    assert 204 not in marked
    cliff = next(point for point in rail.series[0].points if point.id == "color_temp:255:400")
    assert cliff.interest == marked[400]


def test_ct_interest_uses_the_csv_max_brightness_not_the_request_cap(tmp_path: Path) -> None:
    """An EXTEND that lowered max brightness still has the seed's 255-rail cliff."""

    source = tmp_path / "color_temp.csv"
    source.write_text(
        "bri,mired,watt\n"
        + "\n".join(
            f"255,{ct},{watt}"
            for ct, watt in (
                (153, 4.45),
                (180, 4.48),
                (204, 0.20),
                (220, 4.47),
                (250, 4.35),
                (280, 4.50),
                (320, 4.65),
                (370, 4.80),
                (400, 4.60),
                (410, 3.68),
                (420, 3.70),
                (480, 3.20),
                (555, 2.40),
            )
        )
        + "\n199,476,3.1\n199,555,2.4\n",
        encoding="utf-8",
    )
    request = light_request("color_temp")
    request = request.model_copy(
        update={"parameters": replace(request.parameters, max_brightness=199)},
    )

    rail = next(
        plot
        for plot in build_session_plots(request, {"LCT010/color_temp.csv": source}).plots
        if plot.id == "color_temp_max_bri"
    )

    marked = {int(round(1_000_000 / mark.x)): mark.label for mark in rail.markers}
    assert marked[400].startswith("discontinuity at ")
    assert marked[410].startswith("discontinuity at ")


def test_hs_100_percent_rail_marks_smart_interest_points(tmp_path: Path) -> None:
    source = tmp_path / "hs.csv"
    source.write_text(
        "bri,hue,sat,watt\n"
        "255,1,255,1.20\n"
        "255,10923,255,2.10\n"
        "255,21845,255,4.10\n"
        "255,32768,255,2.80\n"
        "255,43690,255,3.30\n"
        "255,54613,255,2.40\n",
        encoding="utf-8",
    )
    request = light_request("hs")

    rail = next(
        plot
        for plot in build_session_plots(request, {"LCT010/hs.csv": source}).plots
        if plot.id == "hs_max_bri"
    )

    marked = {int(round(mark.x / 60) * 60): mark.label for mark in rail.markers}
    assert marked[0].startswith("red primary at 0°")
    assert marked[120].startswith("green primary at 120°")
    assert marked[240].startswith("blue primary at 240°")
    assert any(label.startswith("primary midpoint at ") for label in marked.values())
    red = next(point for point in rail.series[0].points if point.id == "hs:255:1:255")
    assert red.interest == marked[0]


def test_hs_interest_ignores_a_narrowed_request_range(tmp_path: Path) -> None:
    """Partial-result plot of a full LUT must keep RGB ticks when this run sliced hue/sat."""

    source = tmp_path / "hs.csv"
    source.write_text(
        "bri,hue,sat,watt\n"
        "255,1,255,1.20\n"
        "255,10923,255,2.10\n"
        "255,21845,255,4.10\n"
        "255,32768,255,2.80\n"
        "255,43690,255,3.30\n"
        "255,54613,255,2.40\n"
        "255,1,10,4.30\n"
        "255,21845,10,4.32\n"
        "255,40000,10,4.31\n",
        encoding="utf-8",
    )
    request = light_request("hs")
    request = request.model_copy(
        update={"parameters": replace(request.parameters, min_hue=8100, max_hue=40000, max_sat=10)},
    )

    rail = next(
        plot
        for plot in build_session_plots(request, {"LCT010/hs.csv": source}).plots
        if plot.id == "hs_max_bri"
    )

    marked = {int(round(mark.x / 60) * 60): mark.label for mark in rail.markers}
    assert marked[0].startswith("red primary at 0°")
    assert marked[120].startswith("green primary at 120°")
    assert marked[240].startswith("blue primary at 240°")
    assert not any(round(mark.x) in {44, 220} for mark in rail.markers)


def test_brightness_axis_uses_configured_range_when_points_are_only_at_max(tmp_path: Path) -> None:
    source = tmp_path / "color_temp.csv"
    source.write_text("bri,mired,watt\n255,150,4.3\n255,350,4.5\n255,500,2.4\n", encoding="utf-8")

    plot = build_session_plots(light_request("color_temp"), {"LCT010/color_temp.csv": source}).plots[0]

    assert plot.id == "color_temp"
    assert plot.x_min == pytest.approx(1 / 255 * 100)
    assert plot.x_max == pytest.approx(100)


def test_brightness_axis_widens_to_existing_data_outside_the_configured_range(tmp_path: Path) -> None:
    source = tmp_path / "brightness.csv"
    source.write_text("bri,watt\n1,0.4\n255,8.0\n", encoding="utf-8")
    request = light_request("brightness")
    request = request.model_copy(
        update={"parameters": replace(request.parameters, min_brightness=50, max_brightness=200)},
    )

    plot = build_session_plots(request, {"LCT010/brightness.csv": source}).plots[0]

    assert plot.x_min == pytest.approx(1 / 255 * 100)
    assert plot.x_max == pytest.approx(100)


def test_hs_point_color_reflects_hue_and_saturation_not_brightness(tmp_path: Path) -> None:
    """A brightness sweep at one hue/saturation should render as one consistent colour.

    Regression test: the HS colour swatch fed HA's brightness into HLS *lightness*, which
    washes every colour out to white at full brightness (lightness 1.0) regardless of hue
    or saturation -- exactly what the CT branch right above it in `core.py` does not do,
    since `_mired_color` never factors brightness in at all.
    """

    source = tmp_path / "hs.csv"
    # Full-saturation red (hue 0) at three brightness levels -- 10%, 50%, 100%.
    source.write_text(
        "bri,hue,sat,watt\n25,0,255,0.4\n128,0,255,2.0\n255,0,255,4.0\n",
        encoding="utf-8",
    )

    result = build_session_plots(light_request("hs"), {"LCT010/hs.csv": source})

    colors = [point.color for point in result.plots[0].series[0].points]
    assert colors == [colors[0]] * 3, f"brightness should not change the hue/saturation swatch, got {colors}"
    assert colors[0] == "#ff0000", "full-saturation red should render as pure red, not white or diluted"


def test_refine_seed_keys_are_marked_inherited(tmp_path: Path) -> None:
    from measure.runner.light_plan import HsVariation

    source = tmp_path / "hs.csv"
    source.write_text(
        "bri,hue,sat,watt\n128,1,255,2.0\n128,21845,255,2.2\n",
        encoding="utf-8",
    )
    result = build_session_plots(
        light_request("hs"),
        {"LCT010/hs.csv": source},
        inherited_keys={LutMode.HS: {HsVariation(bri=128, hue=1, sat=255)}},
    )
    points = result.plots[0].series[0].points
    assert points[0].inherited is True
    assert points[1].inherited is False


def test_hs_point_color_fades_to_white_as_saturation_drops() -> None:
    """Lower saturation at the same hue should fade toward white, at any brightness.

    This is what rules out HLS lightness=1 as a fix: it renders the pure hue at *every*
    saturation, never fading toward white the way HSV value=1 does as saturation falls.
    """

    full = _light_point({"bri": "255", "hue": "0", "sat": "255", "watt": "1"}, LutMode.HS)
    half = _light_point({"bri": "255", "hue": "0", "sat": "128", "watt": "1"}, LutMode.HS)
    none = _light_point({"bri": "255", "hue": "0", "sat": "0", "watt": "1"}, LutMode.HS)

    assert full is not None
    assert half is not None
    assert none is not None
    assert full.color == "#ff0000"
    assert none.color == "#ffffff"
    assert full.color != half.color != none.color


def test_prefers_plain_csv_when_compressed_copy_is_also_present(tmp_path: Path) -> None:
    plain = tmp_path / "brightness.csv"
    compressed = tmp_path / "brightness.csv.gz"
    plain.write_text("bri,watt\n1,1.0\n", encoding="utf-8")
    with gzip.open(compressed, "wt", encoding="utf-8") as file:
        file.write("bri,watt\n1,99.0\n")

    result = build_session_plots(
        light_request("brightness"),
        {
            "LCT010/brightness.csv.gz": compressed,
            "LCT010/brightness.csv": plain,
        },
    )

    assert result.plots[0].series[0].points[0].y == pytest.approx(1.0)
    assert result.plots[0].source == "LCT010/brightness.csv"


@pytest.mark.parametrize(
    "device_type, title, x_label",
    [
        ("smart_speaker", "Speaker calibration", "Volume (%)"),
        ("fan", "Fan calibration", "Fan speed (%)"),
        ("vacuum_robot", "Charging calibration", "Battery level (%)"),
        ("unknown", "Linear calibration", "Value"),
    ],
)
def test_builds_linear_model_plot_with_device_specific_labels(
    tmp_path: Path,
    device_type: str,
    title: str,
    x_label: str,
) -> None:
    model = tmp_path / "model.json"
    model.write_text(
        json.dumps(
            {
                "device_type": device_type,
                "calculation_strategy": "linear",
                "linear_config": {"calibrate": ["100 -> 8.5", "0 -> 0.4", "50 -> 4.2"]},
            },
        ),
        encoding="utf-8",
    )

    plot = build_plot_from_file(model)

    assert plot.title == title
    assert plot.x_label == x_label
    assert [point.x for point in plot.series[0].points] == [0.0, 50.0, 100.0]


def test_builds_composite_model_plot_from_nested_linear_calibration(tmp_path: Path) -> None:
    model = tmp_path / "model.json"
    model.write_text(
        json.dumps(
            {
                "device_type": "vacuum_robot",
                "calculation_strategy": "composite",
                "composite_config": [
                    {"fixed": {"power": 648}},
                    {
                        "condition": {"condition": "state", "entity_id": "sensor.battery", "state": "on"},
                        "linear": {"calibrate": ["15 -> 17.31", "100 -> 8.14"]},
                    },
                    {"fixed": {"power": 2.6}},
                ],
            },
        ),
        encoding="utf-8",
    )

    plot = build_plot_from_file(model)

    assert plot.title == "Charging calibration"
    assert len(plot.series) == 1
    assert plot.series[0].label is None
    assert [(point.x, point.y) for point in plot.series[0].points] == [(15.0, 17.31), (100.0, 8.14)]


def test_builds_composite_model_plot_with_labelled_series(tmp_path: Path) -> None:
    model = tmp_path / "model.json"
    model.write_text(
        json.dumps(
            {
                "device_type": "fan",
                "calculation_strategy": "composite",
                "composite_config": {
                    "mode": "stop_at_first",
                    "strategies": [
                        {
                            "condition": {
                                "condition": "and",
                                "conditions": [
                                    {
                                        "condition": "state",
                                        "entity_id": "[[entity]]",
                                        "attribute": "oscillating",
                                        "state": True,
                                    },
                                    {
                                        "condition": "state",
                                        "entity_id": "[[entity_by_translation_key:direction]]",
                                        "state": "reverse",
                                    },
                                ],
                            },
                            "linear": {"calibrate": ["50 -> 8.06", "100 -> 28.48"]},
                        },
                        {"linear": {"calibrate": ["50 -> 6.25", "100 -> 27.36"]}},
                    ],
                },
            },
        ),
        encoding="utf-8",
    )

    plot = build_plot_from_file(model)

    assert plot.title == "Fan calibration"
    assert [series.label for series in plot.series] == [
        "oscillating = true AND direction = reverse",
        "Unconditional",
    ]
    assert plot.series[0].color != plot.series[1].color


def test_rejects_composite_model_without_linear_calibration(tmp_path: Path) -> None:
    model = tmp_path / "model.json"
    model.write_text(
        json.dumps(
            {
                "calculation_strategy": "composite",
                "composite_config": [{"fixed": {"power": 2.6}}],
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(PlotDataError, match="does not contain linear calibration data"):
        build_plot_from_file(model)


@pytest.mark.parametrize(
    "model_data",
    [
        None,
        {"calculation_strategy": "fixed"},
        {"calculation_strategy": "composite"},
        {"calculation_strategy": "composite", "composite_config": {}},
        {"calculation_strategy": "composite", "composite_config": [None, {"fixed": {"power": 1}}]},
    ],
)
def test_model_without_usable_linear_calibration_is_not_supported(model_data: object) -> None:
    assert model_has_linear_calibration(model_data) is False


def test_rejects_composite_model_without_valid_calibration_points(tmp_path: Path) -> None:
    model = tmp_path / "model.json"
    model.write_text(
        json.dumps(
            {
                "calculation_strategy": "composite",
                "composite_config": [{"linear": {"calibrate": [None, "invalid", "nan -> 1"]}}],
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(PlotDataError, match="no valid linear calibration entries found"):
        build_plot_from_file(model)


def test_composite_series_labels_handle_supported_condition_shapes(tmp_path: Path) -> None:
    model = tmp_path / "model.json"
    model.write_text(
        json.dumps(
            {
                "calculation_strategy": "composite",
                "composite_config": [
                    {
                        "condition": {
                            "condition": "not",
                            "conditions": [
                                {"condition": "state", "entity_id": ["switch.pump"], "state": ["on", "starting"]},
                            ],
                        },
                        "linear": {"calibrate": ["0 -> 1", "100 -> 2"]},
                    },
                    {
                        "condition": {"condition": "numeric_state", "entity_id": "sensor.speed", "above": 0},
                        "linear": {"calibrate": ["0 -> 2", "100 -> 3"]},
                    },
                    {
                        "condition": {"condition": "state", "entity_id": "[[entity]]", "state": "docked"},
                        "linear": {"calibrate": ["0 -> 3", "100 -> 4"]},
                    },
                    {
                        "condition": {"condition": "state", "state": "on"},
                        "linear": {"calibrate": ["0 -> 4", "100 -> 5"]},
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    plot = build_plot_from_file(model)

    assert [series.label for series in plot.series] == [
        "NOT (switch.pump = on, starting)",
        "Strategy 2",
        "state = docked",
        "Strategy 4",
    ]


def test_builds_recorder_time_series_and_ignores_invalid_rows(tmp_path: Path) -> None:
    recording = tmp_path / "record.csv"
    recording.write_text("0.0,1.2\ninvalid,row\n2.0,3.4\n3.0,nan\n", encoding="utf-8")
    request = parse_measurement_request(
        {
            "measure_type": "recorder",
            "model_id": "measurement",
            "power_meter": {"type": "dummy"},
            "export_filename": "record.csv",
        },
    )

    result = build_session_plots(request, {"measurement/record.csv": recording})

    assert result.warnings == ()
    assert result.plots[0].kind is PlotKind.LINE
    assert result.plots[0].x_label == "Elapsed time (s)"
    assert [(point.x, point.y) for point in result.plots[0].series[0].points] == [(0.0, 1.2), (2.0, 3.4)]


def test_builds_complex_recorder_time_series_from_json_lines(tmp_path: Path) -> None:
    recording = tmp_path / "record.jsonl"
    recording.write_text(
        """{"record_type":"metadata","format_version":1,"primary_entity_id":"switch.plug","entities":[]}
{"record_type":"sample","elapsed_seconds":0.0,"power":1.2,"entities":{"switch.plug":{"state":"on","attributes":{}}}}
incomplete
{"elapsed_seconds":2.0,"power":3.4,"entities":{}}
{"elapsed_seconds":3.0,"power":"nan","entities":{}}
""",
        encoding="utf-8",
    )
    request = parse_measurement_request(
        {
            "measure_type": "recorder",
            "model_id": "measurement",
            "power_meter": {"type": "dummy"},
            "recorder_purpose": "complex_profile",
            "profile_recipe": "generic",
            "tracked_entity_ids": ["switch.plug"],
            "export_filename": "record.jsonl",
        },
    )

    result = build_session_plots(request, {"measurement/record.jsonl": recording})

    assert result.warnings == ()
    assert [(point.x, point.y) for point in result.plots[0].series[0].points] == [(0.0, 1.2), (2.0, 3.4)]


def test_downsamples_large_recorder_files_while_streaming(tmp_path: Path) -> None:
    recording = tmp_path / "record.csv"
    recording.write_text(
        "\n".join(f"{index},{999 if index == 10_000 else index % 20}" for index in range(20_000)),
        encoding="utf-8",
    )
    request = parse_measurement_request(
        {
            "measure_type": "recorder",
            "model_id": "measurement",
            "power_meter": {"type": "dummy"},
            "export_filename": "record.csv",
        },
    )

    with patch("measure.visualization.core._limit_line", side_effect=AssertionError("full input was materialized")):
        result = build_session_plots(
            request,
            {"measurement/record.csv": recording},
            max_line_points=8,
        )

    points = result.plots[0].series[0].points
    assert len(points) <= 8
    assert points[0].x == 0
    assert points[-1].x == 19_999
    assert max(point.y for point in points) == 999


def test_reports_invalid_artifact_without_hiding_other_plots(tmp_path: Path) -> None:
    brightness = tmp_path / "brightness.csv"
    color_temp = tmp_path / "color_temp.csv"
    brightness.write_text("bri,watt\n1,1.0\n", encoding="utf-8")
    color_temp.write_text("wrong,headers\n1,2\n", encoding="utf-8")

    result = build_session_plots(
        light_request("brightness", "color_temp"),
        {
            "LCT010/brightness.csv": brightness,
            "LCT010/color_temp.csv": color_temp,
        },
    )

    assert [plot.id for plot in result.plots] == ["brightness"]
    assert len(result.warnings) == 1
    assert "color_temp.csv" in result.warnings[0]


def test_a_row_with_a_missing_trailing_value_from_a_still_running_write_is_skipped(tmp_path: Path) -> None:
    # Simulates polling a light's CSV while it's still being written for a live preview
    # (see docs/integrations.md "live-plot"): a trailing row's numeric field can be empty
    # if a flush landed between fields. Already handled gracefully -- the missing value
    # just drops that one point, same as any other unreadable row -- with no special
    # casing needed for a session that's still RUNNING versus one that's finished.
    brightness = tmp_path / "brightness.csv"
    brightness.write_text("bri,watt\n1,1.0\n2,\n", encoding="utf-8")

    result = build_session_plots(
        light_request("brightness"),
        {"LCT010/brightness.csv": brightness},
    )

    assert not result.warnings
    assert len(result.plots) == 1
    assert [point.x for point in result.plots[0].series[0].points] == [pytest.approx(1 / 255 * 100)]


def test_reads_effect_csv_with_utf8_bom(tmp_path: Path) -> None:
    effect = tmp_path / "effect.csv.gz"
    with gzip.open(effect, "wt", encoding="utf-8-sig") as file:
        file.write("effect,bri,watt\nnone,5,3.85\nnone,15,4.72\n")

    plot = build_plot_from_file(effect)

    assert plot.id == "effect"
    assert [series.label for series in plot.series] == ["none"]


def test_limits_line_plot_points_without_losing_extrema(tmp_path: Path) -> None:
    recording = tmp_path / "record.csv"
    recording.write_text(
        "\n".join(f"{index},{100 if index == 50 else index % 7}" for index in range(100)),
        encoding="utf-8",
    )

    plot = build_plot_from_file(recording, max_points=20)

    assert len(plot.series[0].points) <= 20
    assert max(point.y for point in plot.series[0].points) == 100
