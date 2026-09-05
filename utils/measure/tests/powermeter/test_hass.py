from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from measure.home_assistant import HomeAssistantManager
from measure.powermeter.errors import OutdatedMeasurementError, PowerMeterError, UnsupportedFeatureError
from measure.powermeter.hass import HassPowerMeter
import pytest

NOW = datetime(2026, 9, 5, 17, 0, 0, tzinfo=UTC)


def _state(value: str, *, reported: datetime | None, updated: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(state=value, last_reported=reported, last_updated=updated)


def _meter(home_assistant: MagicMock, **kwargs: object) -> HassPowerMeter:
    return HassPowerMeter(home_assistant, False, entity_id="sensor.power", clock=lambda: NOW.timestamp(), **kwargs)  # type: ignore[arg-type]


def test_reading_timestamp_prefers_last_reported_over_last_updated() -> None:
    home_assistant = MagicMock(spec=HomeAssistantManager)
    reported = datetime(2026, 9, 5, 16, 59, 58, tzinfo=UTC)
    updated = datetime(2026, 9, 5, 16, 50, 0, tzinfo=UTC)
    home_assistant.get_state.return_value = _state("3.766", reported=reported, updated=updated)

    result = _meter(home_assistant).get_power()

    assert result.power == 3.766
    assert result.updated == reported.timestamp()


def test_reading_timestamp_falls_back_to_last_updated_then_clock() -> None:
    home_assistant = MagicMock(spec=HomeAssistantManager)
    updated = datetime(2026, 9, 5, 16, 50, 0, tzinfo=UTC)
    home_assistant.get_state.return_value = _state("1.0", reported=None, updated=updated)
    assert _meter(home_assistant).get_power().updated == updated.timestamp()

    home_assistant.get_state.return_value = _state("1.0", reported=None, updated=None)
    assert _meter(home_assistant).get_power().updated == NOW.timestamp()


def test_max_age_rejects_a_sensor_that_stopped_reporting() -> None:
    home_assistant = MagicMock(spec=HomeAssistantManager)
    reported = datetime(2026, 9, 5, 16, 54, 0, tzinfo=UTC)  # 6 minutes ago
    home_assistant.get_state.return_value = _state("3.766", reported=reported, updated=reported)

    with pytest.raises(OutdatedMeasurementError, match="last reported 360s ago, more than the allowed 60s"):
        _meter(home_assistant, max_age_seconds=60).get_power()


def test_max_age_accepts_a_fresh_report_and_is_off_by_default() -> None:
    home_assistant = MagicMock(spec=HomeAssistantManager)
    reported = datetime(2026, 9, 5, 16, 59, 30, tzinfo=UTC)
    home_assistant.get_state.return_value = _state("3.766", reported=reported, updated=reported)

    assert _meter(home_assistant, max_age_seconds=60).get_power().power == 3.766

    old = datetime(2026, 9, 1, tzinfo=UTC)
    home_assistant.get_state.return_value = _state("3.766", reported=old, updated=old)
    assert _meter(home_assistant).get_power().power == 3.766


def test_unavailable_sensor_and_voltage_handling() -> None:
    home_assistant = MagicMock(spec=HomeAssistantManager)
    home_assistant.get_state.return_value = _state("unavailable", reported=NOW, updated=NOW)
    with pytest.raises(PowerMeterError, match="unavailable"):
        _meter(home_assistant).get_power()

    home_assistant.get_state.return_value = _state("2.5", reported=NOW, updated=NOW)
    with pytest.raises(UnsupportedFeatureError):
        _meter(home_assistant).get_power(include_voltage=True)

    home_assistant.get_state.side_effect = [
        _state("2.5", reported=NOW, updated=NOW),
        _state("231.6", reported=NOW, updated=NOW),
    ]
    result = _meter(home_assistant, voltage_entity_id="sensor.voltage").get_power(include_voltage=True)
    assert result.voltage == 231.6

    home_assistant.get_state.side_effect = [
        _state("2.5", reported=NOW, updated=NOW),
        _state("unavailable", reported=NOW, updated=NOW),
    ]
    with pytest.raises(PowerMeterError, match=r"Voltage sensor sensor.voltage unavailable"):
        _meter(home_assistant, voltage_entity_id="sensor.voltage").get_power(include_voltage=True)


def test_has_voltage_support_checks_the_voltage_entity() -> None:
    home_assistant = MagicMock(spec=HomeAssistantManager)
    assert _meter(home_assistant).has_voltage_support() is False

    home_assistant.get_state.return_value = _state("230.1", reported=NOW, updated=NOW)
    assert _meter(home_assistant, voltage_entity_id="sensor.voltage").has_voltage_support() is True

    home_assistant.get_state.return_value = _state("unavailable", reported=NOW, updated=NOW)
    with pytest.raises(PowerMeterError, match="unavailable"):
        _meter(home_assistant, voltage_entity_id="sensor.voltage").has_voltage_support()


def test_call_update_entity_triggers_the_service_and_waits() -> None:
    home_assistant = MagicMock(spec=HomeAssistantManager)
    home_assistant.get_state.return_value = _state("2.5", reported=NOW, updated=NOW)
    wait = MagicMock()
    meter = HassPowerMeter(home_assistant, True, entity_id="sensor.power", wait=wait)

    meter.get_power()

    home_assistant.trigger_service.assert_called_once_with("homeassistant", "update_entity", entity_id="sensor.power")
    wait.assert_called_once_with(1)


def test_diagnostic_sample_uses_reported_timestamp_and_rejects_unknown() -> None:
    home_assistant = MagicMock(spec=HomeAssistantManager)
    reported = datetime(2026, 9, 5, 16, 59, 0, tzinfo=UTC)
    home_assistant.get_state.return_value = _state("2.50", reported=reported, updated=NOW)
    sample = _meter(home_assistant).diagnostic_sample()
    assert sample.raw_value == "2.50"
    assert sample.reported_at == reported.timestamp()

    home_assistant.get_state.return_value = _state("2.50", reported=None, updated=None)
    assert _meter(home_assistant).diagnostic_sample().reported_at == NOW.timestamp()

    home_assistant.get_state.return_value = _state("unknown", reported=NOW, updated=NOW)
    with pytest.raises(PowerMeterError, match="unknown"):
        _meter(home_assistant).diagnostic_sample()
