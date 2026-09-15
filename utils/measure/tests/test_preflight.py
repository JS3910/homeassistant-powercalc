from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from measure.controller.charging.spec import HassChargingControllerSpec
from measure.controller.fan.spec import HassFanControllerSpec
from measure.controller.light.const import LutMode
from measure.controller.light.dummy import DummyLightController
from measure.controller.light.spec import (
    DummyLightControllerSpec,
    HassLightControllerSpec,
    HassMultiLightControllerSpec,
)
from measure.controller.media.spec import HassMediaControllerSpec
from measure.ha_app.preflight import (
    ActiveSessionError,
    EntityRecord,
    HistoricalRunTiming,
    MeasurementPreflight,
    PreflightError,
    estimate_light_measurement,
    historical_seconds_per_point,
)
from measure.home_assistant_entities import DeviceClass
from measure.powermeter.diagnostics import DiagnosticStatus, PowerMeterDiagnostic
from measure.powermeter.spec import (
    CompositePowerMeterSpec,
    DummyPowerMeterSpec,
    HassPowerMeterSpec,
    ManualPowerMeterSpec,
    ShellyPowerMeterSpec,
    WitnessSpec,
)
from measure.request import (
    AverageMeasurementRequest,
    ChargingMeasurementRequest,
    DummyLoadCalibrationRequest,
    FanMeasurementRequest,
    LightMeasurementRequest,
    RecorderMeasurementRequest,
    ResumePolicy,
    SpeakerMeasurementRequest,
)
from measure.runner.light_plan import Variation
from measure.runner.smart_envelope import estimate_smart_mode, phase_a_variations
import pytest


@dataclass
class Entity(EntityRecord):
    entity_id: str
    supported_modes: list[LutMode] | None = None
    effect_list: list[str] | None = None
    min_mired: int | None = None
    max_mired: int | None = None
    state: str = "available"
    attribute_names: list[str] = field(default_factory=list)
    device_id: str | None = None
    model_id: str | None = None
    member_entity_ids: list[str] = field(default_factory=list)
    domain: str = ""
    device_class: DeviceClass | None = None


def preflight(
    entities: dict[tuple[str | None, str | None], list[Entity]],
    *,
    active: bool = False,
    writable: bool = True,
    voltage_supported: bool | None = True,
    developer_mode: bool = True,
    load_measured_variations: Callable[[str], Mapping[LutMode, Collection[Variation]]] | None = None,
) -> MeasurementPreflight:
    def verify() -> None:
        if not writable:
            raise OSError("read only")

    return MeasurementPreflight(
        has_active_session=lambda: active,
        verify_storage=verify,
        load_entities=lambda domain, device_class: entities.get((domain, device_class), []),
        load_all_entities=lambda: list(
            {entity.entity_id: entity for group in entities.values() for entity in group}.values(),
        ),
        diagnose_power_meter=lambda _: PowerMeterDiagnostic(
            success=voltage_supported is not None,
            status=DiagnosticStatus.GOOD if voltage_supported is not None else DiagnosticStatus.POOR,
            precision_status=DiagnosticStatus.UNSUPPORTED,
            update_interval_status=DiagnosticStatus.UNSUPPORTED,
            supports_voltage=voltage_supported,
            message="Could not inspect voltage capability" if voltage_supported is None else None,
        ),
        developer_mode=developer_mode,
        load_measured_variations=load_measured_variations,
    )


def base_entities() -> dict[tuple[str | None, str | None], list[Entity]]:
    return {
        (None, "power"): [Entity("sensor.power")],
        (None, "voltage"): [Entity("sensor.voltage")],
        ("light", None): [Entity("light.test", [LutMode.BRIGHTNESS])],
        ("media_player", None): [Entity("media_player.test")],
        ("fan", None): [Entity("fan.test")],
        ("vacuum", None): [Entity("vacuum.test", attribute_names=["battery_level"])],
        ("lawn_mower", None): [Entity("lawn_mower.test", attribute_names=["battery_level"])],
        ("sensor", None): [Entity("sensor.battery", state="75")],
    }


@pytest.mark.parametrize(
    "payload",
    [
        AverageMeasurementRequest(power_meter=HassPowerMeterSpec(entity_id="sensor.power")),
        SpeakerMeasurementRequest(
            power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
            controller=HassMediaControllerSpec(entity_id="media_player.test"),
        ),
        FanMeasurementRequest(
            power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
            controller=HassFanControllerSpec(entity_id="fan.test"),
        ),
        ChargingMeasurementRequest(
            power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
            controller=HassChargingControllerSpec(entity_id="vacuum.test"),
            charging_device_type="vacuum_robot",
        ),
    ],
)
def test_preflight_validates_runtime_dependencies_for_every_non_light_kind(payload: Any) -> None:  # noqa: ANN401
    assert preflight(base_entities()).validate(payload).warnings == ()


def test_preflight_rejects_missing_hass_power_entity_for_non_light_kind() -> None:
    request = AverageMeasurementRequest(power_meter=HassPowerMeterSpec(entity_id="sensor.missing"))
    checker = preflight(base_entities())

    with pytest.raises(PreflightError, match="power entity"):
        checker.validate(request)


def test_preflight_rejects_a_power_meter_type_the_app_cannot_build() -> None:
    """Manual power meters block on a console prompt the app has no console to answer."""
    request = AverageMeasurementRequest(power_meter=ManualPowerMeterSpec())
    checker = preflight(base_entities())

    with pytest.raises(PreflightError, match="not supported by the Home Assistant app"):
        checker.validate(request)


def test_preflight_accepts_a_composite_power_meter_with_a_hass_primary() -> None:
    """A composite must not be rejected outright -- its primary drives whether it's supported."""
    request = AverageMeasurementRequest(
        power_meter=CompositePowerMeterSpec(
            primary=HassPowerMeterSpec(entity_id="sensor.power"),
            witnesses=[WitnessSpec(meter=ShellyPowerMeterSpec(device_ip="192.168.1.50"))],
        ),
    )

    assert preflight(base_entities()).validate(request).warnings == ()


def test_preflight_rejects_a_composite_power_meter_whose_primary_is_unsupported() -> None:
    request = AverageMeasurementRequest(
        power_meter=CompositePowerMeterSpec(
            primary=ManualPowerMeterSpec(),
            witnesses=[WitnessSpec(meter=ShellyPowerMeterSpec(device_ip="192.168.1.50"))],
        ),
    )
    checker = preflight(base_entities())

    with pytest.raises(PreflightError, match="not supported by the Home Assistant app"):
        checker.validate(request)


def test_preflight_rejects_a_missing_hass_power_entity_behind_a_composite_primary() -> None:
    request = AverageMeasurementRequest(
        power_meter=CompositePowerMeterSpec(
            primary=HassPowerMeterSpec(entity_id="sensor.missing"),
            witnesses=[WitnessSpec(meter=ShellyPowerMeterSpec(device_ip="192.168.1.50"))],
        ),
    )
    checker = preflight(base_entities())

    with pytest.raises(PreflightError, match="power entity"):
        checker.validate(request)


def test_preflight_rejects_a_missing_hass_power_entity_behind_a_composite_witness() -> None:
    request = AverageMeasurementRequest(
        power_meter=CompositePowerMeterSpec(
            primary=ShellyPowerMeterSpec(device_ip="192.168.1.50"),
            witnesses=[WitnessSpec(meter=HassPowerMeterSpec(entity_id="sensor.missing"))],
        ),
    )
    checker = preflight(base_entities())

    with pytest.raises(PreflightError, match="power entity"):
        checker.validate(request)


def test_preflight_accepts_vacuum_recorder_with_same_device_battery() -> None:
    entities = base_entities()
    vacuum = Entity("vacuum.test", device_id="robot-device", domain="vacuum")
    battery = Entity(
        "sensor.robot_battery",
        state="42",
        device_id="robot-device",
        domain="sensor",
        device_class=DeviceClass.BATTERY,
    )
    entities[("vacuum", None)] = [vacuum]
    entities[(None, "battery")] = [battery]
    request = RecorderMeasurementRequest(
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        recorder_purpose="complex_profile",
        profile_recipe="vacuum_robot",
        vacuum_entity_id=vacuum.entity_id,
        battery_entity_id=battery.entity_id,
    )

    assert preflight(entities).validate(request).warnings == ()


def test_preflight_accepts_playbook_recorder_without_entity_catalog() -> None:
    checker = MeasurementPreflight(
        has_active_session=lambda: False,
        verify_storage=lambda: None,
        load_entities=lambda domain, device_class: base_entities().get((domain, device_class), []),
        diagnose_power_meter=lambda _: PowerMeterDiagnostic(
            success=True,
            status=DiagnosticStatus.GOOD,
            precision_status=DiagnosticStatus.UNSUPPORTED,
            update_interval_status=DiagnosticStatus.UNSUPPORTED,
        ),
        developer_mode=True,
    )

    result = checker.validate(RecorderMeasurementRequest(power_meter=DummyPowerMeterSpec()))

    assert result.warnings == ()


def test_preflight_can_skip_power_meter_diagnostic() -> None:
    """Resume must not block on a live meter reading — OCR has no frame yet at boot."""

    def fail(_: object) -> PowerMeterDiagnostic:
        raise AssertionError("resume must not wait on a live meter reading")

    checker = MeasurementPreflight(
        has_active_session=lambda: False,
        verify_storage=lambda: None,
        load_entities=lambda domain, device_class: base_entities().get((domain, device_class), []),
        diagnose_power_meter=fail,
        developer_mode=True,
    )

    result = checker.validate(
        RecorderMeasurementRequest(power_meter=DummyPowerMeterSpec()),
        skip_power_meter_diagnostic=True,
    )

    assert result.power_meter_diagnostic is None


def test_preflight_requires_entity_catalog_for_complex_recorder() -> None:
    checker = MeasurementPreflight(
        has_active_session=lambda: False,
        verify_storage=lambda: None,
        load_entities=lambda domain, device_class: base_entities().get((domain, device_class), []),
        diagnose_power_meter=lambda _: PowerMeterDiagnostic(
            success=True,
            status=DiagnosticStatus.GOOD,
            precision_status=DiagnosticStatus.UNSUPPORTED,
            update_interval_status=DiagnosticStatus.UNSUPPORTED,
        ),
        developer_mode=True,
    )
    request = RecorderMeasurementRequest(
        power_meter=DummyPowerMeterSpec(),
        recorder_purpose="complex_profile",
        profile_recipe="generic",
        tracked_entity_ids=("switch.plug",),
    )

    with pytest.raises(PreflightError, match="entity metadata is unavailable"):
        checker.validate(request)


def test_preflight_accepts_generic_recorder_entity_from_complete_catalog() -> None:
    entities = base_entities()
    entities[("switch", None)] = [Entity("switch.plug", domain="switch")]
    request = RecorderMeasurementRequest(
        power_meter=DummyPowerMeterSpec(),
        recorder_purpose="complex_profile",
        profile_recipe="generic",
        tracked_entity_ids=("switch.plug",),
    )

    assert preflight(entities).validate(request).warnings == ()


def test_preflight_rejects_missing_complex_recorder_entity() -> None:
    request = RecorderMeasurementRequest(
        power_meter=DummyPowerMeterSpec(),
        recorder_purpose="complex_profile",
        profile_recipe="generic",
        tracked_entity_ids=("switch.missing",),
    )

    checker = preflight(base_entities())
    with pytest.raises(PreflightError, match="does not exist"):
        checker.validate(request)


def test_preflight_rejects_unavailable_vacuum() -> None:
    entities = base_entities()
    # Keep the selected entity in the complete catalog while omitting it from the available vacuum choices.
    entities[(None, None)] = [Entity("vacuum.missing", domain="vacuum")]
    entities[("sensor", None)] = [Entity("sensor.robot_battery", state="42")]
    entities[(None, "battery")] = [Entity("sensor.robot_battery", state="42")]
    request = RecorderMeasurementRequest(
        power_meter=DummyPowerMeterSpec(),
        recorder_purpose="complex_profile",
        profile_recipe="vacuum_robot",
        vacuum_entity_id="vacuum.missing",
        battery_entity_id="sensor.robot_battery",
    )

    checker = preflight(entities)
    with pytest.raises(PreflightError, match="vacuum is unavailable"):
        checker.validate(request)


@pytest.mark.parametrize(
    "battery_group, message",
    [
        ([], "battery sensor is unavailable"),
        ([Entity("sensor.robot_battery", device_id="other-device", state="42")], "same Home Assistant device"),
    ],
)
def test_preflight_rejects_unusable_vacuum_battery(battery_group: list[Entity], message: str) -> None:
    entities = base_entities()
    entities[("vacuum", None)] = [Entity("vacuum.test", device_id="robot-device")]
    entities[(None, "battery")] = battery_group
    # Retain the selection in the complete catalog so this exercises availability or relationship validation.
    entities[("sensor", None)] = battery_group or [Entity("sensor.robot_battery", state="unavailable")]
    request = RecorderMeasurementRequest(
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        recorder_purpose="complex_profile",
        profile_recipe="vacuum_robot",
        vacuum_entity_id="vacuum.test",
        battery_entity_id="sensor.robot_battery",
    )

    checker = preflight(entities)
    with pytest.raises(PreflightError, match=message):
        checker.validate(request)


def test_preflight_requires_voltage_sensor_for_dummy_load() -> None:
    request = AverageMeasurementRequest(
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        dummy_load=DummyLoadCalibrationRequest(description="40 W incandescent bulb"),
    )

    checker = preflight(base_entities())
    with pytest.raises(PreflightError, match="voltage sensor is required"):
        checker.validate(request)


def test_preflight_rejects_power_meter_without_dummy_load_voltage_support() -> None:
    request = AverageMeasurementRequest(
        power_meter=ShellyPowerMeterSpec(device_ip="192.168.1.50"),
        dummy_load=DummyLoadCalibrationRequest(description="40 W incandescent bulb"),
    )

    checker = preflight(base_entities(), voltage_supported=False)
    with pytest.raises(PreflightError, match="does not support voltage"):
        checker.validate(request)


def test_preflight_reports_unknown_dummy_load_voltage_capability() -> None:
    request = AverageMeasurementRequest(
        power_meter=ShellyPowerMeterSpec(device_ip="192.168.1.50"),
        dummy_load=DummyLoadCalibrationRequest(description="40 W incandescent bulb"),
    )

    validator = preflight(base_entities(), voltage_supported=None)
    with pytest.raises(PreflightError, match="Could not inspect voltage capability"):
        validator.validate(request)


def test_dummy_load_sampling_failure_does_not_mask_controller_validation() -> None:
    diagnostic = PowerMeterDiagnostic(
        success=False,
        supports_voltage=True,
        status=DiagnosticStatus.POOR,
        precision_status=DiagnosticStatus.UNSUPPORTED,
        update_interval_status=DiagnosticStatus.UNSUPPORTED,
        message="Could not read power",
    )
    checker = MeasurementPreflight(
        has_active_session=lambda: False,
        verify_storage=lambda: None,
        load_entities=lambda domain, device_class: base_entities().get((domain, device_class), []),
        diagnose_power_meter=lambda _: diagnostic,
        developer_mode=True,
    )
    request = SpeakerMeasurementRequest(
        power_meter=ShellyPowerMeterSpec(device_ip="192.168.1.50"),
        controller=HassMediaControllerSpec(entity_id="media_player.missing"),
        dummy_load=DummyLoadCalibrationRequest(description="40 W incandescent bulb"),
    )

    with pytest.raises(PreflightError, match="media player"):
        checker.validate(request)


def test_preflight_includes_minimum_dummy_load_calibration_duration() -> None:
    request = AverageMeasurementRequest(
        power_meter=HassPowerMeterSpec(
            entity_id="sensor.power",
            voltage_entity_id="sensor.voltage",
        ),
        dummy_load=DummyLoadCalibrationRequest(description="40 W incandescent bulb"),
    )

    result = preflight(base_entities()).validate(request)

    assert result.estimated_duration_seconds == 600
    assert "at least 10 minutes" in result.warnings[0]


@pytest.mark.parametrize(
    "measurement, message",
    [
        (
            LightMeasurementRequest(
                model_id="LCT010",
                product_name="Test light",
                measure_device="Test meter",
                power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
                controller=HassLightControllerSpec(entity_id="light.missing"),
            ),
            "light entity",
        ),
        (
            SpeakerMeasurementRequest(
                power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
                controller=HassMediaControllerSpec(entity_id="media_player.missing"),
            ),
            "media player",
        ),
        (
            FanMeasurementRequest(
                power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
                controller=HassFanControllerSpec(entity_id="fan.missing"),
            ),
            "fan",
        ),
        (
            ChargingMeasurementRequest(
                power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
                controller=HassChargingControllerSpec(entity_id="lawn_mower.missing"),
                charging_device_type="vacuum_robot",
            ),
            "does not match",
        ),
    ],
)
def test_preflight_requires_ha_device_entities(measurement: Any, message: str) -> None:  # noqa: ANN401
    checker = preflight(base_entities())

    with pytest.raises(PreflightError, match=message):
        checker.validate(measurement)


def test_preflight_rejects_charging_type_entity_domain_mismatch() -> None:
    request = ChargingMeasurementRequest(
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassChargingControllerSpec(entity_id="vacuum.test"),
        charging_device_type="lawn_mower_robot",
    )
    checker = preflight(base_entities())

    with pytest.raises(PreflightError, match="does not match"):
        checker.validate(request)


def _charging_request() -> ChargingMeasurementRequest:
    return ChargingMeasurementRequest(
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassChargingControllerSpec(entity_id="vacuum.test"),
        charging_device_type="vacuum_robot",
    )


def test_preflight_rejects_missing_charging_battery_source() -> None:
    """Neither a battery sensor on the device nor the battery_level attribute is available."""
    entities = base_entities() | {("vacuum", None): [Entity("vacuum.test", attribute_names=[])]}

    validator = preflight(entities)
    request = _charging_request()
    with pytest.raises(PreflightError, match=r"battery_level is not available"):
        validator.validate(request)


def test_preflight_accepts_charging_with_related_battery_sensor() -> None:
    """A battery sensor on the same device is used even without the battery_level attribute."""
    entities = base_entities() | {
        ("vacuum", None): [Entity("vacuum.test", attribute_names=[], device_id="vacuum-device")],
        (None, "battery"): [Entity("sensor.vacuum_battery", state="80", device_id="vacuum-device")],
    }

    result = preflight(entities).validate(_charging_request())

    assert result.warnings == ()
    assert result.battery_level_entity_id == "sensor.vacuum_battery"
    assert result.battery_level_attribute is None


def test_preflight_reports_battery_level_attribute_fallback() -> None:
    result = preflight(base_entities()).validate(_charging_request())

    assert result.battery_level_entity_id is None
    assert result.battery_level_attribute == "battery_level"


def test_preflight_rejects_non_numeric_related_battery_sensor() -> None:
    entities = base_entities() | {
        ("vacuum", None): [Entity("vacuum.test", attribute_names=[], device_id="vacuum-device")],
        (None, "battery"): [Entity("sensor.vacuum_battery", state="unknown", device_id="vacuum-device")],
    }

    validator = preflight(entities)
    request = _charging_request()
    with pytest.raises(PreflightError, match="numeric percentage"):
        validator.validate(request)


def test_light_preflight_accepts_dummy_controller_without_entity_checks() -> None:
    request = LightMeasurementRequest(
        model_id="dummy",
        product_name="Virtual light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.BRIGHTNESS},
        parameters={"sleep_time": 0.5, "sample_count": 2},
    )

    result = preflight(base_entities()).validate(request)

    assert result.supported_modes == (LutMode.BRIGHTNESS,)
    assert result.estimated_variations == 255
    assert result.estimated_duration_seconds is not None


def test_light_preflight_returns_supported_modes_and_estimate() -> None:
    request = LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(
            entity_id="sensor.power",
            voltage_entity_id="sensor.voltage",
        ),
        controller=HassLightControllerSpec(entity_id="light.test"),
        modes={LutMode.BRIGHTNESS},
        parameters={"sleep_time": 0.5, "sample_count": 2},
    )

    result = preflight(base_entities()).validate(request)

    assert result.supported_modes == (LutMode.BRIGHTNESS,)
    assert result.estimated_variations == 255
    assert result.estimated_duration_seconds == 782


def test_light_preflight_uses_device_color_temperature_range() -> None:
    entities = base_entities()
    entities[("light", None)] = [
        Entity(
            "light.test",
            [LutMode.COLOR_TEMP],
            min_mired=200,
            max_mired=300,
        ),
    ]
    request = LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassLightControllerSpec(entity_id="light.test"),
        modes={LutMode.COLOR_TEMP},
        parameters={"ct_bri_steps": 10, "ct_mired_divisions": 10},
    )

    result = preflight(entities).validate(request)

    assert result.estimated_variations == 297


def test_light_preflight_uses_default_color_temperature_resolution() -> None:
    entities = base_entities()
    entities[("light", None)] = [
        Entity(
            "light.test",
            [LutMode.COLOR_TEMP],
            min_mired=150,
            max_mired=500,
        ),
    ]
    request = LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassLightControllerSpec(entity_id="light.test"),
        modes={LutMode.COLOR_TEMP},
    )

    result = preflight(entities).validate(request)

    assert result.estimated_variations == 884


def test_light_preflight_accepts_implied_brightness_when_only_color_temp_is_advertised() -> None:
    """HA never lists literal brightness next to color_temp; the UI still checks it."""

    entities = base_entities()
    entities[("light", None)] = [Entity("light.test", [LutMode.COLOR_TEMP])]
    request = LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassLightControllerSpec(entity_id="light.test"),
        modes={LutMode.BRIGHTNESS, LutMode.COLOR_TEMP},
    )

    result = preflight(entities).validate(request)

    assert result.supported_modes == (LutMode.COLOR_TEMP,)


def test_light_preflight_names_the_mode_the_light_does_not_advertise() -> None:
    entities = base_entities()
    entities[("light", None)] = [Entity("light.test", [LutMode.BRIGHTNESS, LutMode.COLOR_TEMP])]
    request = LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassLightControllerSpec(entity_id="light.test"),
        modes={LutMode.COLOR_TEMP, LutMode.EFFECT},
    )

    with pytest.raises(PreflightError, match="does not advertise effect"):
        preflight(entities).validate(request)


def test_hs_preflight_uses_default_native_resolution() -> None:
    entities = base_entities()
    entities[("light", None)] = [Entity("light.test", [LutMode.HS])]
    request = LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassLightControllerSpec(entity_id="light.test"),
        modes={LutMode.HS},
    )

    result = preflight(entities).validate(request)

    assert result.estimated_variations == 1_080


def test_estimate_light_measurement_breaks_down_axes_and_readings() -> None:
    entities = base_entities()
    entities[("light", None)] = [
        Entity("light.test", [LutMode.COLOR_TEMP], min_mired=200, max_mired=300),
    ]
    request = LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassLightControllerSpec(entity_id="light.test"),
        modes={LutMode.COLOR_TEMP},
        parameters={"ct_bri_steps": 10, "ct_mired_divisions": 10, "sample_count": 2},
    )

    result = estimate_light_measurement(
        request,
        load_entities=lambda domain, device_class: entities.get((domain, device_class), []),
    )

    assert result.used_default_range is False
    assert result.modes[0].mode == LutMode.COLOR_TEMP
    assert result.modes[0].points == result.modes[0].axes["mired"] * result.modes[0].axes["brightness"]
    assert result.total_points == result.modes[0].points
    assert result.total_readings == result.total_points * 2
    assert "kelvin" in result.modes[0].summary
    assert result.max_duration_seconds > 0


def test_estimate_smart_sampling_reports_discovery_and_a_coverage_cap() -> None:
    request = LightMeasurementRequest(
        model_id="dummy",
        product_name="Virtual light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.COLOR_TEMP},
        parameters={"smart_sampling": True, "smart_delta": 8},
    )

    result = estimate_light_measurement(request)

    assert result.modes[0].axes["discovery"] == result.modes[0].points
    assert result.modes[0].points > 4
    assert result.modes[0].axes["coverage_cap"] > 0
    assert result.total_points == result.modes[0].points + result.modes[0].axes["coverage_cap"]
    assert "discovery" in result.modes[0].summary
    assert result.max_duration_seconds > 0


def test_estimate_smart_extend_counts_ramps_and_coverage_beyond_the_seed_outline() -> None:
    request = LightMeasurementRequest(
        model_id="dummy",
        product_name="Virtual light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.COLOR_TEMP},
        resume_policy=ResumePolicy.EXTEND,
        seed_session_id="seed-session",
        parameters={"smart_sampling": True, "smart_delta": 8},
    )
    light = DummyLightController().get_light_info()
    outline = phase_a_variations(LutMode.COLOR_TEMP, request.parameters, light)
    estimate = estimate_smart_mode(LutMode.COLOR_TEMP, request.parameters, light)
    expected = estimate.discovery + estimate.coverage_cap - len(outline)

    result = estimate_light_measurement(
        request,
        load_measured_variations=lambda _: {LutMode.COLOR_TEMP: set(outline)},
        load_historical_runs=lambda: [
            HistoricalRunTiming(
                session_id="seed-session",
                model_id="dummy",
                measure_device="Test meter",
                elapsed_seconds=900,
                completed_points=100,
                modes=frozenset({LutMode.COLOR_TEMP}),
            )
        ],
    )

    assert expected > 0
    assert result.remaining_points == expected
    assert result.total_points == expected
    assert result.max_duration_seconds > 0
    assert result.estimated_duration_seconds == expected * 9
    assert result.estimated_from_runs == 1


def test_estimate_smart_extend_remaining_grows_with_discovery_settings() -> None:
    def remaining_for(ct_bri_steps: int) -> int:
        request = LightMeasurementRequest(
            model_id="dummy",
            product_name="Virtual light",
            measure_device="Test meter",
            power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
            controller=DummyLightControllerSpec(),
            modes={LutMode.COLOR_TEMP},
            resume_policy=ResumePolicy.EXTEND,
            seed_session_id="seed-session",
            parameters={"smart_sampling": True, "ct_bri_steps": ct_bri_steps},
        )
        outline = phase_a_variations(
            LutMode.COLOR_TEMP,
            request.parameters,
            DummyLightController().get_light_info(),
        )
        result = estimate_light_measurement(
            request,
            load_measured_variations=lambda _: {LutMode.COLOR_TEMP: set(outline)},
        )
        assert result.remaining_points is not None
        return result.remaining_points

    assert remaining_for(1) > remaining_for(5)


def test_estimate_light_measurement_flags_the_default_range_without_a_light() -> None:
    request = LightMeasurementRequest(
        model_id="dummy",
        product_name="Virtual light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.COLOR_TEMP},
    )

    result = estimate_light_measurement(request)

    assert result.used_default_range is True
    assert result.total_points > 0


def test_estimate_extend_subtracts_seed_keys_without_changing_preflight() -> None:
    request = LightMeasurementRequest(
        model_id="dummy",
        product_name="Virtual light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.BRIGHTNESS},
        resume_policy=ResumePolicy.EXTEND,
        seed_session_id="seed-session",
    )

    result = estimate_light_measurement(
        request,
        load_measured_variations=lambda _: {LutMode.BRIGHTNESS: {Variation(1)}},
    )

    assert result.remaining_points == 254
    assert result.total_points == 254
    assert result.total_readings is None
    assert result.estimated_duration_seconds is None


def test_historical_seconds_per_point_uses_matching_model_and_meter() -> None:
    request = LightMeasurementRequest(
        model_id="36871",
        product_name="KAJPLATS",
        measure_device="Zhurui PR10",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
    )
    matching = HistoricalRunTiming(
        session_id="seed",
        model_id="36871",
        measure_device="Zhurui PR10",
        elapsed_seconds=1800,
        completed_points=200,
        modes=frozenset({LutMode.HS}),
    )
    other_meter = HistoricalRunTiming(
        session_id="other",
        model_id="36871",
        measure_device="Shelly",
        elapsed_seconds=60,
        completed_points=200,
        modes=frozenset({LutMode.HS}),
    )

    rate = historical_seconds_per_point(request, [matching, other_meter])

    assert rate == (9.0, 1)


def test_historical_seconds_per_point_keeps_the_refine_seed_even_when_metadata_differs() -> None:
    request = LightMeasurementRequest(
        model_id="36871",
        product_name="KAJPLATS",
        measure_device="Zhurui PR10",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        resume_policy=ResumePolicy.EXTEND,
        seed_session_id="seed",
    )
    seed = HistoricalRunTiming(
        session_id="seed",
        model_id="old-id",
        measure_device="Old meter name",
        elapsed_seconds=900,
        completed_points=100,
        modes=frozenset({LutMode.COLOR_TEMP}),
    )

    assert historical_seconds_per_point(request, [seed]) == (9.0, 1)


def test_historical_seconds_per_point_ignores_effect_runs_unless_this_request_measures_effects() -> None:
    request = LightMeasurementRequest(
        model_id="36871",
        product_name="KAJPLATS",
        measure_device="Zhurui PR10",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.HS},
    )
    effect = HistoricalRunTiming(
        session_id="fx",
        model_id="36871",
        measure_device="Zhurui PR10",
        elapsed_seconds=3600,
        completed_points=10,
        modes=frozenset({LutMode.EFFECT}),
    )

    assert historical_seconds_per_point(request, [effect]) is None


def test_estimate_uses_historical_rate_for_remaining_refine_points() -> None:
    request = LightMeasurementRequest(
        model_id="dummy",
        product_name="Virtual light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.BRIGHTNESS},
        resume_policy=ResumePolicy.EXTEND,
        seed_session_id="seed-session",
    )
    history = HistoricalRunTiming(
        session_id="seed-session",
        model_id="dummy",
        measure_device="Test meter",
        elapsed_seconds=1270,
        completed_points=254,
        modes=frozenset({LutMode.BRIGHTNESS}),
    )

    result = estimate_light_measurement(
        request,
        load_measured_variations=lambda _: {LutMode.BRIGHTNESS: {Variation(1)}},
        load_historical_runs=lambda: [history],
    )

    assert result.remaining_points == 254
    assert result.estimated_from_runs == 1
    assert result.estimated_duration_seconds == 1270


def test_multi_light_preflight_uses_common_capabilities_and_models() -> None:
    entities = base_entities()
    entities[("light", None)] = [
        Entity(
            "light.one",
            [LutMode.BRIGHTNESS, LutMode.COLOR_TEMP],
            min_mired=150,
            max_mired=400,
            model_id="LWA017",
        ),
        Entity(
            "light.two",
            [LutMode.BRIGHTNESS, LutMode.COLOR_TEMP],
            min_mired=200,
            max_mired=500,
            model_id="LWA017",
        ),
    ]
    request = LightMeasurementRequest(
        model_id="LWA017",
        product_name="Test lights",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassMultiLightControllerSpec(entity_ids=["light.one", "light.two"]),
        modes={LutMode.COLOR_TEMP},
        multiple_light_count=2,
    )

    result = preflight(entities).validate(request)

    assert result.warnings == ()
    assert result.supported_modes == (LutMode.BRIGHTNESS, LutMode.COLOR_TEMP)


def test_multi_light_preflight_rejects_mixed_models_and_group_member_overlap() -> None:
    entities = base_entities()
    entities[("light", None)] = [
        Entity("light.group", [LutMode.BRIGHTNESS], member_entity_ids=["light.one", "light.two"]),
        Entity("light.one", [LutMode.BRIGHTNESS], model_id="ONE"),
        Entity("light.two", [LutMode.BRIGHTNESS], model_id="TWO"),
    ]
    common = {
        "model_id": "ONE",
        "product_name": "Test lights",
        "measure_device": "Test meter",
        "power_meter": HassPowerMeterSpec(entity_id="sensor.power"),
        "multiple_light_count": 3,
    }

    mixed = LightMeasurementRequest(
        **common,
        controller=HassMultiLightControllerSpec(entity_ids=["light.one", "light.two"]),
    )
    with pytest.raises(PreflightError, match="same model ID"):
        preflight(entities).validate(mixed)

    overlap = LightMeasurementRequest(
        **common,
        controller=HassMultiLightControllerSpec(entity_ids=["light.group", "light.one"]),
    )
    with pytest.raises(PreflightError, match="group and one of its members"):
        preflight(entities).validate(overlap)


def test_multi_light_preflight_warns_for_unknown_models_and_validates_count() -> None:
    entities = base_entities()
    entities[("light", None)] = [
        Entity("light.one", [LutMode.BRIGHTNESS]),
        Entity("light.two", [LutMode.BRIGHTNESS]),
    ]
    request = LightMeasurementRequest(
        model_id="manual",
        product_name="Test lights",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassMultiLightControllerSpec(entity_ids=["light.one", "light.two"]),
        multiple_light_count=2,
    )

    assert "Could not confirm" in preflight(entities).validate(request).warnings[0]
    with pytest.raises(PreflightError, match="Number of lights"):
        preflight(entities).validate(request.model_copy(update={"multiple_light_count": 1}))


def test_light_group_does_not_force_its_discovered_member_count() -> None:
    entities = base_entities()
    entities[("light", None)] = [
        Entity(
            "light.group",
            [LutMode.BRIGHTNESS],
            model_id="LWA017",
            member_entity_ids=["light.one", "light.two"],
        ),
        Entity("light.one", [LutMode.BRIGHTNESS], model_id="LWA017"),
        Entity("light.two", [LutMode.BRIGHTNESS], model_id="LWA017"),
    ]
    request = LightMeasurementRequest(
        model_id="LWA017",
        product_name="Test group",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=HassLightControllerSpec(entity_id="light.group"),
        multiple_light_count=1,
    )

    result = preflight(entities).validate(request)
    assert any("2 member lights" in warning for warning in result.warnings)


def test_preflight_reports_active_session_before_external_checks() -> None:
    request = AverageMeasurementRequest(power_meter=DummyPowerMeterSpec())
    checker = preflight({}, active=True)

    with pytest.raises(ActiveSessionError):
        checker.validate(request)


def test_preflight_reports_unwritable_storage() -> None:
    request = AverageMeasurementRequest(power_meter=DummyPowerMeterSpec())
    checker = preflight({}, writable=False)

    with pytest.raises(PreflightError, match="not writable"):
        checker.validate(request)


def test_non_hass_power_meter_does_not_require_power_entity() -> None:
    request = AverageMeasurementRequest(power_meter=DummyPowerMeterSpec())

    result = preflight({}).validate(request)

    assert result.warnings == ()


def test_extend_preflight_subtracts_seed_keys_from_the_remaining_count() -> None:
    request = LightMeasurementRequest(
        model_id="dummy",
        product_name="Virtual light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.BRIGHTNESS},
        parameters={"sleep_time": 0.5, "sample_count": 2},
        resume_policy=ResumePolicy.EXTEND,
        seed_session_id="seed-session",
    )
    full = preflight(base_entities()).validate(
        request.model_copy(update={"resume_policy": ResumePolicy.NEW, "seed_session_id": None}),
    )
    remaining = preflight(
        base_entities(),
        load_measured_variations=lambda _: {LutMode.BRIGHTNESS: {Variation(1)}},
    ).validate(request)

    assert full.estimated_variations == 255
    assert remaining.estimated_variations == 254
    assert remaining.estimated_duration_seconds is not None
    assert full.estimated_duration_seconds is not None
    assert remaining.estimated_duration_seconds < full.estimated_duration_seconds


def test_extend_remeasure_preflight_keeps_the_full_count() -> None:
    request = LightMeasurementRequest(
        model_id="dummy",
        product_name="Virtual light",
        measure_device="Test meter",
        power_meter=HassPowerMeterSpec(entity_id="sensor.power"),
        controller=DummyLightControllerSpec(),
        modes={LutMode.BRIGHTNESS},
        parameters={"sleep_time": 0.5, "sample_count": 2},
        resume_policy=ResumePolicy.EXTEND,
        seed_session_id="seed-session",
        remeasure_existing=True,
    )

    result = preflight(
        base_entities(),
        load_measured_variations=lambda _: {LutMode.BRIGHTNESS: {Variation(1)}},
    ).validate(request)

    assert result.estimated_variations == 255
