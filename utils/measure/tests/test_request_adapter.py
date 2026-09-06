from unittest.mock import MagicMock

from measure.cli.request_adapter import request_from_answers
from measure.const import QUESTION_MEASURE_DEVICE, MeasureType
from measure.controller.light.const import LightControllerType, LutMode
from measure.controller.light.spec import HueLightControllerSpec
from measure.powermeter.const import QUESTION_POWERMETER_ENTITY_ID, PowerMeterType, WitnessPosition
from measure.powermeter.spec import (
    CompositePowerMeterSpec,
    HassPowerMeterSpec,
    OcrPowerMeterSpec,
    ShellyPowerMeterSpec,
    TuyaPowerMeterSpec,
    WitnessSpec,
)
from measure.request import ResumePolicy
from measure.runner.const import QUESTION_DURATION, QUESTION_MODE
import pytest

from tests.conftest import MockConfigFactory


def test_cli_answers_and_environment_become_one_measurement_request(
    mock_config_factory: MockConfigFactory,
) -> None:
    environment = mock_config_factory()
    environment.selected_light_controller = LightControllerType.HUE
    environment.selected_power_meter = PowerMeterType.HASS
    environment.hue_bridge_ip = "192.0.2.10"
    answers = {
        QUESTION_MEASURE_DEVICE: "Test meter",
        QUESTION_MODE: {LutMode.BRIGHTNESS},
        QUESTION_POWERMETER_ENTITY_ID: "sensor.power",
        "light": "group:12",
    }

    request = request_from_answers(MeasureType.LIGHT, answers, environment)

    assert request.power_meter == HassPowerMeterSpec(entity_id="sensor.power")
    assert request.controller == HueLightControllerSpec(bridge_ip="192.0.2.10", light="group:12")
    assert request.parameters.ct_bri_steps == environment.ct_bri_steps


def test_tuya_key_stays_in_cli_config(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.selected_power_meter = PowerMeterType.TUYA
    environment.tuya_device_id = "device-id"
    environment.tuya_device_ip = "192.0.2.20"
    environment.tuya_device_key = "device-key"
    environment.tuya_device_version = "3.4"

    request = request_from_answers(MeasureType.AVERAGE, {QUESTION_DURATION: 60}, environment)

    assert request.power_meter == TuyaPowerMeterSpec(
        device_id="device-id",
        device_ip="192.0.2.20",
        version="3.4",
    )
    assert "device-key" not in request.model_dump_json()


def test_shelly_password_stays_in_cli_config(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.selected_power_meter = PowerMeterType.SHELLY
    environment.shelly_ip = "192.0.2.30"
    environment.shelly_username = "measurement"
    environment.shelly_password = "device-password"  # noqa: S105
    environment.shelly_timeout = 10

    request = request_from_answers(MeasureType.AVERAGE, {QUESTION_DURATION: 60}, environment)

    assert request.power_meter == ShellyPowerMeterSpec(
        device_ip="192.0.2.30",
        username="measurement",
        timeout=10,
    )
    assert "device-password" not in request.model_dump_json()


def test_ocr_power_meter_spec_comes_from_the_environment(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.selected_power_meter = PowerMeterType.OCR
    environment.ocr_source = "http://camera.local:8080/"
    environment.ocr_layout = "pr10"
    environment.ocr_preview_host = "0.0.0.0"  # noqa: S104
    environment.ocr_preview_port = None
    environment.ocr_window_seconds = 2.0
    environment.ocr_stale_after_seconds = 8.0
    environment.ocr_crosscheck_tolerance_pct = 4.5

    request = request_from_answers(MeasureType.AVERAGE, {QUESTION_DURATION: 60}, environment)

    assert request.power_meter == OcrPowerMeterSpec(
        source="http://camera.local:8080/",
        layout="pr10",
        preview_host="0.0.0.0",  # noqa: S104
        preview_port=None,
        window_seconds=2.0,
        stale_after_seconds=8.0,
        crosscheck_tolerance_pct=4.5,
    )


def test_witness_meters_wrap_the_primary_in_a_composite(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.selected_power_meter = PowerMeterType.OCR
    environment.witness_meters = [PowerMeterType.SHELLY, PowerMeterType.HASS]
    environment.shelly_ip = "192.0.2.30"
    environment.shelly_username = "admin"
    environment.shelly_timeout = 5
    environment.hass_call_update_entity_service = False
    environment.hass_max_age_seconds = 30.0
    environment.witness_position = MagicMock(
        side_effect=lambda t: WitnessPosition.AFTER_PRIMARY if t == PowerMeterType.SHELLY else WitnessPosition.NONE,
    )
    environment.witness_offset_w = MagicMock(side_effect=lambda t: 2.45 if t == PowerMeterType.SHELLY else 0.0)
    environment.witness_tolerance_w = MagicMock(return_value=0.5)
    environment.witness_tolerance_pct = MagicMock(return_value=2.0)
    environment.witness_required = MagicMock(side_effect=lambda t: t == PowerMeterType.SHELLY)
    answers = {QUESTION_DURATION: 60, QUESTION_POWERMETER_ENTITY_ID: "sensor.power"}

    request = request_from_answers(MeasureType.AVERAGE, answers, environment)

    assert request.power_meter == CompositePowerMeterSpec(
        primary=OcrPowerMeterSpec(),
        witnesses=[
            WitnessSpec(
                meter=ShellyPowerMeterSpec(device_ip="192.0.2.30", username="admin", timeout=5),
                position=WitnessPosition.AFTER_PRIMARY,
                offset_w=2.45,
                tolerance_w=0.5,
                tolerance_pct=2.0,
                required=True,
            ),
            WitnessSpec(
                meter=HassPowerMeterSpec(entity_id="sensor.power", max_age_seconds=30.0),
                position=WitnessPosition.NONE,
                offset_w=0.0,
                tolerance_w=0.5,
                tolerance_pct=2.0,
                required=False,
            ),
        ],
    )


def test_hass_max_age_reaches_the_spec(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.selected_power_meter = PowerMeterType.HASS
    environment.hass_call_update_entity_service = False
    environment.hass_max_age_seconds = 120.0

    request = request_from_answers(
        MeasureType.AVERAGE,
        {QUESTION_DURATION: 60, QUESTION_POWERMETER_ENTITY_ID: "sensor.power"},
        environment,
    )

    assert request.power_meter == HassPowerMeterSpec(entity_id="sensor.power", max_age_seconds=120.0)


def test_composite_is_not_a_selectable_primary(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.selected_power_meter = PowerMeterType.COMPOSITE

    with pytest.raises(ValueError, match="POWER_METER=composite is not a meter"):
        request_from_answers(MeasureType.AVERAGE, {QUESTION_DURATION: 60}, environment)


def test_cli_resume_setting_becomes_request_policy(mock_config_factory: MockConfigFactory) -> None:
    environment = mock_config_factory()
    environment.resume = True

    request = request_from_answers(
        MeasureType.AVERAGE,
        {QUESTION_DURATION: 60, "model_id": "existing-run"},
        environment,
    )

    assert request.resume_policy == ResumePolicy.RESUME
