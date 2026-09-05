import json
from pathlib import Path

from measure.model import mains_voltage_from_range, write_model_json
from measure.powermeter.spec import (
    CompositePowerMeterSpec,
    DummyPowerMeterSpec,
    HassPowerMeterSpec,
    ShellyPowerMeterSpec,
    WitnessSpec,
)
from measure.tuning import MeasurementParameters
import pytest


def base_parameters() -> MeasurementParameters:
    return MeasurementParameters(sample_count=1, sleep_time=2)


@pytest.mark.parametrize(
    "voltage_range, expected",
    [
        ({"min": 118.0, "max": 122.0}, 120),
        ({"min": 226.0, "max": 234.0}, 230),
        ({"min": 100.0, "max": 260.0}, 230),  # midpoint 180, closer to 230 by construction below
        (None, None),
        ("not-a-dict", None),
        ({"min": True, "max": 120.0}, None),
        ({"min": 130.0, "max": 100.0}, None),
    ],
)
def test_mains_voltage_from_range_handles_edge_cases(voltage_range: object, expected: int | None) -> None:
    assert mains_voltage_from_range(voltage_range) == expected


def test_write_model_json_omits_witnesses_for_a_single_meter(tmp_path: Path) -> None:
    write_model_json(
        tmp_path,
        standby_power=0.3,
        name="Test device",
        measure_device="Test meter",
        parameters=base_parameters(),
        power_meter=DummyPowerMeterSpec(),
    )

    model = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert "WITNESSES" not in model["measure_settings"]


def test_write_model_json_omits_witnesses_when_no_power_meter_is_given(tmp_path: Path) -> None:
    write_model_json(
        tmp_path,
        standby_power=0.3,
        name="Test device",
        measure_device="Test meter",
        parameters=base_parameters(),
    )

    model = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert "WITNESSES" not in model["measure_settings"]


def test_write_model_json_records_every_witness_field_in_measure_settings(tmp_path: Path) -> None:
    power_meter = CompositePowerMeterSpec(
        primary=HassPowerMeterSpec(entity_id="sensor.power"),
        witnesses=[
            WitnessSpec(
                meter=ShellyPowerMeterSpec(device_ip="192.0.2.50"),
                offset_w=2.5,
                tolerance_w=0.75,
                tolerance_pct=4.0,
                required=False,
            ),
        ],
    )

    write_model_json(
        tmp_path,
        standby_power=0.3,
        name="Test device",
        measure_device="Test meter",
        parameters=base_parameters(),
        power_meter=power_meter,
    )

    model = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert model["measure_settings"]["WITNESSES"] == [
        {"type": "shelly", "offset_w": 2.5, "tolerance_w": 0.75, "tolerance_pct": 4.0, "required": False},
    ]


def test_write_model_json_still_records_the_usual_fields_alongside_witnesses(tmp_path: Path) -> None:
    power_meter = CompositePowerMeterSpec(
        primary=HassPowerMeterSpec(entity_id="sensor.power"),
        witnesses=[WitnessSpec(meter=ShellyPowerMeterSpec(device_ip="192.0.2.50"))],
    )

    path = write_model_json(
        tmp_path,
        standby_power=1.2,
        name="Test device",
        measure_device="Test meter",
        parameters=base_parameters(),
        power_meter=power_meter,
        voltages=[229.5, 230.5],
        dummy_load=True,
        dummy_load_resistance=1000.0,
    )

    model = json.loads(path.read_text(encoding="utf-8"))
    assert model["standby_power"] == pytest.approx(1.2)
    assert model["measure_settings"]["DUMMY_LOAD"] is True
    assert model["measure_settings"]["DUMMY_LOAD_RESISTANCE"] == pytest.approx(1000.0)
    assert len(model["measure_settings"]["WITNESSES"]) == 1


def test_write_model_json_extra_data_is_not_shadowed_by_witnesses(tmp_path: Path) -> None:
    power_meter = CompositePowerMeterSpec(
        primary=HassPowerMeterSpec(entity_id="sensor.power"),
        witnesses=[WitnessSpec(meter=ShellyPowerMeterSpec(device_ip="192.0.2.50"))],
    )

    write_model_json(
        tmp_path,
        standby_power=0.3,
        name="Test device",
        measure_device="Test meter",
        parameters=base_parameters(),
        power_meter=power_meter,
        extra_json_data={"device_type": "generic"},
    )

    model = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert model["device_type"] == "generic"
    assert len(model["measure_settings"]["WITNESSES"]) == 1
