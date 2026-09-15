from pathlib import Path

from measure.controller.light.const import LutMode
from measure.execution import LightOperatingPoint
from measure.powermeter.spec import DummyPowerMeterSpec
from measure.request import AverageMeasurementRequest, LightMeasurementRequest
from measure.runner.light_plan import ColorTempVariation, HsVariation, Variation
from measure.runner.sweep_coverage import (
    build_sweep_coverage,
    has_complete_lut_row,
    load_measured_variations,
)
from measure.tuning import MeasurementParameters


def _light_request(**overrides: object) -> LightMeasurementRequest:
    payload: dict[str, object] = {
        "measure_device": "Test meter",
        "controller": {"type": "dummy"},
        "power_meter": DummyPowerMeterSpec(),
        "modes": {LutMode.COLOR_TEMP},
        "parameters": MeasurementParameters(
            min_brightness=1,
            max_brightness=3,
            ct_bri_steps=2,
            ct_mired_divisions=3,
            hs_bri_steps=2,
            hs_hue_divisions=3,
            hs_sat_divisions=2,
            effect_bri_steps=1,
            min_hue=1,
            max_hue=6,
            min_sat=1,
            max_sat=2,
        ),
    }
    payload.update(overrides)
    return LightMeasurementRequest.model_validate(payload)


def test_non_light_session_has_no_coverage() -> None:
    request = AverageMeasurementRequest.model_validate(
        {"power_meter": DummyPowerMeterSpec(), "duration": 10},
    )
    assert build_sweep_coverage(request, []) is None


def test_brightness_only_session_has_empty_coverage() -> None:
    request = _light_request(modes={LutMode.BRIGHTNESS})
    assert build_sweep_coverage(request, [Variation(bri=1)]) == {}


def test_color_temp_tick_is_done_only_when_every_planned_brightness_exists() -> None:
    request = _light_request()
    coverage = build_sweep_coverage(
        request,
        [
            ColorTempVariation(bri=1, ct=150),
            ColorTempVariation(bri=3, ct=150),
        ],
    )
    assert coverage is not None
    ticks = {tick["value"]: tick["status"] for tick in coverage["color_temp"]}
    assert ticks[150] == "done"
    assert 500 in ticks
    assert ticks[500] == "pending"


def test_color_temp_tick_is_current_while_that_mired_sweep_is_in_progress() -> None:
    request = _light_request()
    coverage = build_sweep_coverage(
        request,
        [ColorTempVariation(bri=1, ct=150)],
        LightOperatingPoint(type="light", on=True, brightness=3, color_temp_mired=150),
    )
    assert coverage is not None
    ticks = {tick["value"]: tick["status"] for tick in coverage["color_temp"]}
    assert ticks[150] == "current"


def test_color_temp_stays_current_while_the_light_is_still_on_that_mired() -> None:
    request = _light_request()
    coverage = build_sweep_coverage(
        request,
        [
            ColorTempVariation(bri=1, ct=150),
            ColorTempVariation(bri=3, ct=150),
        ],
        LightOperatingPoint(type="light", on=True, brightness=1, color_temp_mired=150),
    )
    assert coverage is not None
    ticks = {tick["value"]: tick["status"] for tick in coverage["color_temp"]}
    assert ticks[150] == "current"


def test_extra_csv_mired_appears_as_inherited() -> None:
    request = _light_request()
    coverage = build_sweep_coverage(request, [ColorTempVariation(bri=9, ct=370)])
    assert coverage is not None
    ticks = {tick["value"]: tick["status"] for tick in coverage["color_temp"]}
    assert ticks[370] == "inherited"


def test_hue_is_done_only_when_every_planned_sat_and_brightness_exists() -> None:
    request = _light_request(modes={LutMode.HS})
    planned = build_sweep_coverage(request, [])
    assert planned is not None
    first_hue = int(planned["hue"][0]["value"])
    first_sat = int(planned["saturation"][0]["value"])
    coverage = build_sweep_coverage(
        request,
        [
            HsVariation(bri=1, hue=first_hue, sat=first_sat),
        ],
    )
    assert coverage is not None
    hue = {tick["value"]: tick["status"] for tick in coverage["hue"]}
    sat = {tick["value"]: tick["status"] for tick in coverage["saturation"]}
    assert hue[first_hue] == "partial"
    assert sat[first_sat] == "partial"


def test_color_temp_tick_is_partial_when_some_brightness_exists() -> None:
    request = _light_request()
    coverage = build_sweep_coverage(request, [ColorTempVariation(bri=1, ct=150)])
    assert coverage is not None
    ticks = {tick["value"]: tick["status"] for tick in coverage["color_temp"]}
    assert ticks[150] == "partial"


def test_hue_and_sat_mark_current_independently() -> None:
    request = _light_request(modes={LutMode.HS})
    planned = build_sweep_coverage(request, [])
    assert planned is not None
    first_hue = planned["hue"][0]["value"]
    first_sat = planned["saturation"][0]["value"]
    coverage = build_sweep_coverage(
        request,
        [],
        LightOperatingPoint(
            type="light",
            on=True,
            brightness=1,
            hue=int(first_hue),
            saturation=int(first_sat),
        ),
    )
    assert coverage is not None
    hue = {tick["value"]: tick["status"] for tick in coverage["hue"]}
    sat = {tick["value"]: tick["status"] for tick in coverage["saturation"]}
    assert hue[first_hue] == "current"
    assert sat[first_sat] == "current"


def test_off_light_does_not_mark_a_current_tick() -> None:
    request = _light_request()
    coverage = build_sweep_coverage(
        request,
        [],
        LightOperatingPoint(type="light", on=False, color_temp_mired=150),
    )
    assert coverage is not None
    assert all(tick["status"] != "current" for tick in coverage["color_temp"])


def test_load_measured_variations_skips_torn_rows(tmp_path: Path) -> None:
    path = tmp_path / "color_temp.csv"
    path.write_text("bri,mired,watt\n1,150,1.0\n3,150,\n", encoding="utf-8")
    loaded = load_measured_variations(tmp_path, {LutMode.COLOR_TEMP})
    assert loaded == [ColorTempVariation(bri=1, ct=150)]
    assert has_complete_lut_row(tmp_path, {LutMode.COLOR_TEMP})
