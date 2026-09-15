from measure.cli.request_adapter import request_from_answers
from measure.const import PARAMETER_LIMITS, QUESTION_ENTITY_ID, QUESTION_MEASURE_DEVICE, MeasureType
from measure.controller.light.const import LightControllerType, LutMode
from measure.controller.light.spec import HassMultiLightControllerSpec
from measure.powermeter.const import PowerMeterType
from measure.powermeter.spec import DummyPowerMeterSpec
from measure.request import (
    _BASE_PARAMETER_FIELDS,
    _LIGHT_PARAMETER_FIELDS,
    AverageMeasurementRequest,
    DummyLoadCalibrationRequest,
    DummyLoadReuseRequest,
    LightMeasurementRequest,
    RecorderMeasurementRequest,
    RecorderProfileRecipe,
    RecorderPurpose,
    ResumePolicy,
    parse_measurement_request,
)
from measure.runner.const import QUESTION_MODE
from pydantic import ValidationError
import pytest

from tests.conftest import MockConfigFactory


def valid_request() -> dict[str, object]:
    return {
        "model_id": "LCT010",
        "product_name": "Test light",
        "measure_device": "Test meter",
        "controller": {"type": "hass", "entity_id": "light.test"},
        "power_meter": {"type": "hass", "entity_id": "sensor.test_power"},
    }


def test_request_round_trip_preserves_typed_input() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "parameters": {"sleep_time": 0.5, "sample_count": 3},
            "dummy_load": {"mode": "reuse", "description": "60 W incandescent bulb", "resistance": 812.4},
        },
    )

    restored = parse_measurement_request(request.model_dump(mode="json"))

    assert restored == request
    assert isinstance(restored.dummy_load, DummyLoadReuseRequest)


def test_request_exposes_the_controlled_entities_only_when_the_controller_drives_them() -> None:
    controlled = LightMeasurementRequest.model_validate(valid_request())
    uncontrolled = AverageMeasurementRequest.model_validate(
        {"power_meter": {"type": "hass", "entity_id": "sensor.test_power"}},
    )

    assert controlled.controlled_entity_ids == ("light.test",)
    assert uncontrolled.controlled_entity_ids == ()


def test_request_exposes_all_controlled_entities_for_multi_light_controller() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {"controller": {"type": "hass_multi", "entity_ids": ["light.one", "light.two"]}, "multiple_light_count": 2},
    )

    assert isinstance(request.controller, HassMultiLightControllerSpec)
    assert request.controlled_entity_ids == ("light.one", "light.two")


@pytest.mark.parametrize(
    "entity_ids",
    [["light.one"], ["light.one", "light.one"], ["light.one", "switch.two"]],
)
def test_multi_light_controller_requires_multiple_unique_light_entities(entity_ids: list[str]) -> None:
    with pytest.raises(ValidationError):
        LightMeasurementRequest.model_validate(
            valid_request() | {"controller": {"type": "hass_multi", "entity_ids": entity_ids}},
        )


def test_request_normalizes_profile_metadata() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request() | {"product_name": "  Test light  ", "measure_device": "  Test meter  "},
    )

    assert request.product_name == "Test light"
    assert request.measure_device == "Test meter"


@pytest.mark.parametrize("field", ["measure_device"])
def test_request_rejects_blank_required_profile_metadata(field: str) -> None:
    payload = valid_request() | {field: "   "}
    with pytest.raises(ValidationError, match=field):
        LightMeasurementRequest.model_validate(payload)


def test_request_defers_unknown_product_details_until_preparation() -> None:
    payload = valid_request()
    del payload["model_id"]
    del payload["product_name"]
    request = LightMeasurementRequest.model_validate(payload | {"session_name": " Desk lamp "})
    assert request.model_id == ""
    assert request.product_name == ""
    assert request.session_name == "Desk lamp"


def test_request_accepts_dummy_load_calibration() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request() | {"dummy_load": {"mode": "calibrate", "description": "60 W incandescent bulb"}},
    )

    assert request.dummy_load == DummyLoadCalibrationRequest(description="60 W incandescent bulb")


@pytest.mark.parametrize(
    "payload",
    [
        valid_request() | {"measure_type": "light"},
        {"measure_type": "average"},
        {"measure_type": "recorder"},
        {"measure_type": "speaker", "controller": {"type": "dummy"}},
        {
            "measure_type": "charging",
            "controller": {"type": "dummy"},
            "charging_device_type": "vacuum_robot",
        },
        {"measure_type": "fan", "controller": {"type": "dummy"}},
    ],
)
def test_all_measurement_types_accept_dummy_load(payload: dict[str, object]) -> None:
    request = parse_measurement_request(
        payload
        | {
            "power_meter": {
                "type": "hass",
                "entity_id": "sensor.test_power",
                "voltage_entity_id": "sensor.test_voltage",
            },
            "dummy_load": {
                "mode": "reuse",
                "description": "60 W incandescent bulb",
                "resistance": 812.4,
            },
        },
    )

    assert request.dummy_load == DummyLoadReuseRequest(
        description="60 W incandescent bulb",
        resistance=812.4,
    )


def test_request_normalizes_and_requires_dummy_load_description() -> None:
    assert DummyLoadCalibrationRequest(description="  reference load  ").description == "reference load"
    with pytest.raises(ValidationError, match="description"):
        DummyLoadCalibrationRequest(description=" ")


@pytest.mark.parametrize("resistance", [0, -1])
def test_request_rejects_non_positive_dummy_load_resistance(resistance: float) -> None:
    payload = valid_request() | {
        "dummy_load": {
            "mode": "reuse",
            "description": "60 W incandescent bulb",
            "resistance": resistance,
        },
    }
    with pytest.raises(ValidationError, match="resistance"):
        LightMeasurementRequest.model_validate(payload)


def test_request_rejects_dummy_load_with_synthetic_power_meter() -> None:
    power_meter = DummyPowerMeterSpec()
    dummy_load = DummyLoadCalibrationRequest(description="test load")
    with pytest.raises(ValidationError, match="synthetic"):
        AverageMeasurementRequest(power_meter=power_meter, dummy_load=dummy_load)


def test_cli_request_contains_only_resolved_measurement_input(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.selected_power_meter = PowerMeterType.DUMMY
    environment.selected_light_controller = LightControllerType.DUMMY
    environment.resume = True
    answers = {
        QUESTION_ENTITY_ID: "light.hue_test",
        QUESTION_MEASURE_DEVICE: "Test meter",
        QUESTION_MODE: {LutMode.BRIGHTNESS},
        "hue_group": "Kitchen",
    }

    request = request_from_answers(MeasureType.LIGHT, answers, environment)

    assert request.power_meter.type == PowerMeterType.DUMMY
    assert "hue_group" not in request.model_dump()
    assert request.model_id == ""
    assert request.product_name == ""
    assert request.resume_policy == "new"
    existing = request_from_answers(MeasureType.LIGHT, answers | {"model_id": "LCT010"}, environment)
    assert existing.model_id == "LCT010"
    assert existing.resume_policy == "resume"


@pytest.mark.parametrize("model_id", ["../secret", "/unsafe/file", "a/b", ".."])
def test_request_rejects_unsafe_model_id(model_id: str) -> None:
    payload = valid_request() | {"model_id": model_id}

    with pytest.raises(ValidationError):
        LightMeasurementRequest.model_validate(payload)


def test_request_rejects_unknown_fields() -> None:
    payload = valid_request() | {"token": "secret"}

    with pytest.raises(ValidationError):
        LightMeasurementRequest.model_validate(payload)


def test_playbook_recorder_uses_a_fixed_export_filename() -> None:
    request = RecorderMeasurementRequest(power_meter=DummyPowerMeterSpec(), export_filename="custom.csv")

    assert request.export_filename == "record.csv"


def test_recorder_defaults_to_legacy_playbook_recording() -> None:
    request = RecorderMeasurementRequest(power_meter=DummyPowerMeterSpec())

    assert request.recorder_purpose == RecorderPurpose.PLAYBOOK
    assert request.recorded_entity_ids == ()


def test_generic_recorder_preserves_tracked_entity_order() -> None:
    request = RecorderMeasurementRequest(
        power_meter=DummyPowerMeterSpec(),
        recorder_purpose=RecorderPurpose.COMPLEX_PROFILE,
        profile_recipe=RecorderProfileRecipe.GENERIC,
        tracked_entity_ids=("switch.plug", "sensor.mode"),
    )

    assert request.recorded_entity_ids == ("switch.plug", "sensor.mode")
    assert request.export_filename == "record.jsonl"


def test_complex_recorder_uses_a_fixed_export_filename() -> None:
    request = RecorderMeasurementRequest(
        power_meter=DummyPowerMeterSpec(),
        recorder_purpose=RecorderPurpose.COMPLEX_PROFILE,
        profile_recipe=RecorderProfileRecipe.GENERIC,
        tracked_entity_ids=("switch.plug",),
        export_filename="custom.csv",
    )

    assert request.export_filename == "record.jsonl"


def test_vacuum_recorder_orders_required_roles_before_additional_entities() -> None:
    request = RecorderMeasurementRequest(
        power_meter=DummyPowerMeterSpec(),
        recorder_purpose=RecorderPurpose.COMPLEX_PROFILE,
        profile_recipe=RecorderProfileRecipe.VACUUM_ROBOT,
        vacuum_entity_id="vacuum.robot",
        battery_entity_id="sensor.robot_battery",
        additional_entity_ids=("sensor.dock_state",),
    )

    assert request.recorded_entity_ids == ("vacuum.robot", "sensor.robot_battery", "sensor.dock_state")


@pytest.mark.parametrize(
    "values",
    [
        {"profile_recipe": "generic", "tracked_entity_ids": ["switch.plug"]},
        {"recorder_purpose": "complex_profile"},
        {"recorder_purpose": "complex_profile", "profile_recipe": "generic"},
        {
            "recorder_purpose": "complex_profile",
            "profile_recipe": "generic",
            "tracked_entity_ids": ["switch.plug"],
            "vacuum_entity_id": "vacuum.robot",
        },
        {"recorder_purpose": "complex_profile", "profile_recipe": "vacuum_robot"},
        {
            "recorder_purpose": "complex_profile",
            "profile_recipe": "vacuum_robot",
            "tracked_entity_ids": ["switch.plug"],
            "vacuum_entity_id": "vacuum.robot",
            "battery_entity_id": "sensor.robot_battery",
        },
        {
            "recorder_purpose": "complex_profile",
            "profile_recipe": "vacuum_robot",
            "vacuum_entity_id": "switch.robot",
            "battery_entity_id": "sensor.robot_battery",
        },
        {
            "recorder_purpose": "complex_profile",
            "profile_recipe": "vacuum_robot",
            "vacuum_entity_id": "vacuum.robot",
            "battery_entity_id": "binary_sensor.robot_battery",
        },
        {
            "recorder_purpose": "complex_profile",
            "profile_recipe": "vacuum_robot",
            "vacuum_entity_id": "vacuum.robot",
            "battery_entity_id": "sensor.robot_battery",
            "additional_entity_ids": ["sensor.robot_battery"],
        },
        {
            "recorder_purpose": "complex_profile",
            "profile_recipe": "generic",
            "tracked_entity_ids": ["invalid entity"],
        },
    ],
)
def test_recorder_rejects_inconsistent_profile_selections(values: dict[str, object]) -> None:
    payload = {"power_meter": DummyPowerMeterSpec(), **values}
    with pytest.raises(ValidationError):
        RecorderMeasurementRequest.model_validate(payload)


def test_recorder_rejects_more_than_one_hundred_combined_entities() -> None:
    additional_entities = tuple(f"sensor.extra_{index}" for index in range(99))
    power_meter = DummyPowerMeterSpec()

    with pytest.raises(ValidationError, match="at most 100 entities"):
        RecorderMeasurementRequest(
            power_meter=power_meter,
            recorder_purpose=RecorderPurpose.COMPLEX_PROFILE,
            profile_recipe=RecorderProfileRecipe.VACUUM_ROBOT,
            vacuum_entity_id="vacuum.robot",
            battery_entity_id="sensor.robot_battery",
            additional_entity_ids=additional_entities,
        )


def test_cli_recorder_request_uses_fixed_export_filename(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.selected_power_meter = PowerMeterType.DUMMY
    request = request_from_answers(MeasureType.RECORDER, {}, environment)

    assert request.export_filename == "record.csv"


def test_request_preserves_subsecond_sleep_time() -> None:
    request = AverageMeasurementRequest(
        power_meter=DummyPowerMeterSpec(),
        parameters={"sleep_time": 0.25, "sleep_time_sample": 0.5},
    )

    assert request.parameters.sleep_time == pytest.approx(0.25)
    assert request.parameters.sleep_time_sample == pytest.approx(0.5)


def test_legacy_hs_sat_steps_is_dropped_rather_than_treated_as_a_sweep_count() -> None:
    request = LightMeasurementRequest.model_validate(valid_request() | {"parameters": {"hs_sat_steps": 32}})

    assert request.parameters.hs_sat_divisions == 5


@pytest.mark.parametrize(
    "parameters, modes, message",
    [
        ({"sleep_time_sample": -1}, None, "sleep_time_sample"),
        ({"max_retries": 101}, None, "max_retries"),
        ({"max_nudges": 21}, None, "max_nudges"),
        ({"min_brightness": 0}, None, "min_brightness"),
        ({"min_sat": 0}, None, "min_sat"),
        ({"max_hue": 65_536}, None, "max_hue"),
        ({"bri_bri_steps": 0}, None, "bri_bri_steps"),
        ({"ct_bri_steps": 256}, ["color_temp"], "ct_bri_steps"),
        ({"ct_mired_divisions": 2001}, ["color_temp"], "ct_mired_divisions"),
        ({"hs_hue_divisions": 65_536}, ["hs"], "hs_hue_divisions"),
        ({"hs_hue_divisions": 25}, ["hs"], "hs_hue_divisions must be a multiple of 3"),
        ({"hs_sat_divisions": 256}, ["hs"], "hs_sat_divisions"),
        ({"measure_time_effect": 10, "measure_time_effect_min": 20}, ["effect"], "measure_time_effect_min"),
        ({"min_sat": 200, "max_sat": 50}, None, "min_sat must not exceed max_sat"),
        ({"min_hue": 500, "max_hue": 100}, None, "min_hue must not exceed max_hue"),
        ({"min_kelvin": 4000, "max_kelvin": 2500}, ["color_temp"], "min_kelvin must not exceed max_kelvin"),
        ({"min_kelvin": 1000}, ["color_temp"], "min_kelvin"),
        ({"smart_delta": 1}, ["color_temp"], "smart_delta"),
        ({"smart_delta": 50}, ["hs"], "smart_delta"),
        ({"smart_border_delta": 1}, ["color_temp"], "smart_border_delta"),
        ({"smart_border_delta": 50}, ["hs"], "smart_border_delta"),
    ],
)
def test_request_rejects_invalid_exposed_tuning(
    parameters: dict[str, int],
    modes: list[str] | None,
    message: str,
) -> None:
    payload = valid_request() | {"parameters": parameters}
    if modes is not None:
        payload = payload | {"modes": modes}

    with pytest.raises(ValidationError, match=message):
        LightMeasurementRequest.model_validate(payload)


@pytest.mark.parametrize(
    "modes, parameters, leftover",
    [
        (
            ["hs"],
            {"min_brightness": 255, "max_brightness": 255, "hs_bri_steps": 32, "hs_hue_divisions": 6},
            "hs_bri_steps",
        ),
        (
            ["hs"],
            {"min_sat": 255, "max_sat": 255, "hs_sat_divisions": 5, "hs_hue_divisions": 6},
            "hs_sat_divisions",
        ),
        (
            ["hs"],
            {"min_hue": 21845, "max_hue": 21845, "hs_hue_divisions": 25},
            "hs_hue_divisions",
        ),
        (
            ["color_temp"],
            {"min_kelvin": 2700, "max_kelvin": 2700, "ct_mired_divisions": 17},
            "ct_mired_divisions",
        ),
    ],
)
def test_single_point_axis_accepts_leftover_sweep_count(
    modes: list[str],
    parameters: dict[str, int],
    leftover: str,
) -> None:
    """A one-point rail makes the leftover default unused, not illegal."""

    request = LightMeasurementRequest.model_validate(
        valid_request() | {"modes": modes, "parameters": parameters},
    )

    assert getattr(request.parameters, leftover) == parameters[leftover]


def test_unused_effect_step_does_not_block_a_narrow_brightness_range() -> None:
    """Raising min brightness must not reject the unused Effect default of 40."""

    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "modes": ["color_temp", "hs"],
            "parameters": {
                "min_brightness": 223,
                "max_brightness": 255,
                "ct_bri_steps": 33,
                "ct_bri_all": True,
                "hs_bri_steps": 8,
                "hs_hue_divisions": 6,
                "effect_bri_steps": 40,
            },
        },
    )

    assert request.parameters.effect_bri_steps == 40
    assert request.parameters.min_brightness == 223


def test_selected_effect_step_must_fit_the_live_brightness_span() -> None:
    with pytest.raises(ValidationError, match="effect_bri_steps"):
        LightMeasurementRequest.model_validate(
            valid_request()
            | {
                "modes": ["effect"],
                "parameters": {
                    "min_brightness": 223,
                    "max_brightness": 255,
                    "effect_bri_steps": 40,
                },
            },
        )


def test_legacy_native_increment_fields_are_dropped_and_defaults_apply() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "modes": ["color_temp", "hs"],
            "parameters": {
                "ct_mired_steps": 10,
                "hs_hue_steps": 2731,
                "hs_sat_steps": 32,
            },
        },
    )

    assert request.parameters.ct_mired_divisions == 17
    assert request.parameters.hs_hue_divisions == 24
    assert request.parameters.hs_sat_divisions == 5


def test_legacy_increment_fields_do_not_override_current_division_counts() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "modes": ["color_temp", "hs"],
            "parameters": {
                "ct_mired_steps": 10,
                "ct_mired_divisions": 8,
                "hs_hue_steps": 2731,
                "hs_hue_divisions": 6,
                "hs_sat_steps": 32,
                "hs_sat_divisions": 4,
            },
        },
    )

    assert request.parameters.ct_mired_divisions == 8
    assert request.parameters.hs_hue_divisions == 6
    assert request.parameters.hs_sat_divisions == 4


def test_live_sat_span_accepts_a_sweep_count_above_the_old_static_cap() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request() | {"modes": ["hs"], "parameters": {"hs_sat_divisions": 80}},
    )

    assert request.parameters.hs_sat_divisions == 80


def test_all_skips_numeric_validation_for_that_axis() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "modes": ["hs"],
            "parameters": {
                "hs_sat_divisions": 0,
                "hs_sat_all": True,
                "hs_hue_divisions": 25,
                "hs_hue_all": True,
            },
        },
    )

    assert request.parameters.hs_sat_all is True
    assert request.parameters.hs_hue_all is True


@pytest.mark.parametrize(
    "power_meter, accepted",
    [
        ({"type": "manual"}, False),
        ({"type": "hass", "entity_id": "sensor.test_power"}, True),
    ],
)
def test_manual_power_meter_caps_the_ct_grid_finer_than_automated(power_meter: dict[str, str], accepted: bool) -> None:
    # Manual entry caps how *fine* the ct grid can go (15 steps / 9 divisions max) because
    # hand-reading every point is laborious -- an automated session can go much finer.
    payload = valid_request() | {
        "modes": ["color_temp"],
        "power_meter": power_meter,
        "parameters": {"ct_bri_steps": 50, "ct_mired_divisions": 20},
    }

    if accepted:
        request = LightMeasurementRequest.model_validate(payload)
        assert request.parameters.ct_bri_steps == 50
        assert request.parameters.ct_mired_divisions == 20
    else:
        with pytest.raises(ValidationError, match="ct_bri_steps"):
            LightMeasurementRequest.model_validate(payload)


def test_settle_tolerance_pct_is_rejected_with_a_manual_power_meter() -> None:
    with pytest.raises(ValidationError, match="settle_tolerance_pct requires a polled power meter"):
        LightMeasurementRequest.model_validate(
            valid_request()
            | {
                "power_meter": {"type": "manual"},
                "parameters": {"settle_tolerance_pct": 2.0, "ct_mired_divisions": 3},
            },
        )


def test_settle_tolerance_pct_is_allowed_with_a_polled_power_meter() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request() | {"parameters": {"settle_tolerance_pct": 2.0}},
    )

    assert request.parameters.settle_tolerance_pct == 2.0
    assert request.parameters.settle_tolerance_w == pytest.approx(0.1)
    assert request.parameters.settle_min_wait == pytest.approx(2.0)


def test_rated_power_w_fills_an_unbounded_ocr_primarys_plausibility_bound() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "power_meter": {"type": "ocr", "source": "http://camera/mjpeg"},
            "rated_power_w": 10.0,
            "multiple_light_count": 2,
        },
    )

    assert request.power_meter.max_plausible_power_w == 60.0  # 10 W x 2 lights x 3.0 margin


def test_rated_power_w_fills_witnesses_too_but_not_the_primary_if_its_not_ocr() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "power_meter": {
                "type": "composite",
                "primary": {"type": "hass", "entity_id": "sensor.test_power"},
                "witnesses": [{"meter": {"type": "ocr", "source": "http://camera/mjpeg"}}],
            },
            "rated_power_w": 5.0,
        },
    )

    assert request.power_meter.witnesses[0].meter.max_plausible_power_w == 15.0


def test_rated_power_w_does_not_override_an_explicit_bound() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "power_meter": {
                "type": "ocr",
                "source": "http://camera/mjpeg",
                "max_plausible_power_w": 42.0,
            },
            "rated_power_w": 10.0,
        },
    )

    assert request.power_meter.max_plausible_power_w == 42.0


def test_without_rated_power_w_ocr_meters_get_no_plausibility_bound() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request() | {"power_meter": {"type": "ocr", "source": "http://camera/mjpeg"}},
    )

    assert request.power_meter.max_plausible_power_w is None


def test_extend_requires_a_seed_session_id() -> None:
    with pytest.raises(ValidationError, match="seed_session_id"):
        LightMeasurementRequest.model_validate(valid_request() | {"resume_policy": "extend"})


def test_seed_session_id_is_rejected_unless_the_policy_is_extend() -> None:
    with pytest.raises(ValidationError, match="seed_session_id"):
        LightMeasurementRequest.model_validate(
            valid_request() | {"resume_policy": "new", "seed_session_id": "seed-1"},
        )


def test_extend_round_trips_seed_and_derived_from() -> None:
    request = LightMeasurementRequest.model_validate(
        valid_request()
        | {
            "resume_policy": "extend",
            "seed_session_id": "seed-1",
            "remeasure_existing": True,
            "derived_from": ["seed-1"],
        },
    )

    restored = parse_measurement_request(request.model_dump(mode="json"))

    assert restored.resume_policy == ResumePolicy.EXTEND
    assert restored.seed_session_id == "seed-1"
    assert restored.remeasure_existing is True
    assert restored.derived_from == ("seed-1",)


def test_parameter_limits_cover_exactly_the_validated_fields() -> None:
    assert set(_BASE_PARAMETER_FIELDS) | set(_LIGHT_PARAMETER_FIELDS) == set(PARAMETER_LIMITS)
