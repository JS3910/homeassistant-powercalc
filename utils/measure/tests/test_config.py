import logging

from measure.cli.environment import CliEnvironment, uses_power_meter
from measure.const import PARAMETER_LIMITS, MeasureType
from measure.powermeter.const import PowerMeterType
import pytest

# Fields without their own env var: bri_bri_steps is fixed, the hs_*_steps are
# derived from the HS_*_PRECISION vars and cannot leave their table range.
_DERIVED_LIMIT_FIELDS = {"bri_bri_steps", "hs_bri_steps", "hs_hue_steps", "hs_sat_steps"}
_ENV_BACKED_LIMIT_FIELDS = sorted(set(PARAMETER_LIMITS) - _DERIVED_LIMIT_FIELDS)


def test_cli_environment_preserves_manual_power_meter_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWER_METER", PowerMeterType.MANUAL)
    monkeypatch.setenv("SAMPLE_COUNT", "9")
    monkeypatch.setenv("CT_BRI_STEPS", "2")
    monkeypatch.setenv("CT_MIRED_STEPS", "3")

    config = CliEnvironment()

    assert config.selected_power_meter == PowerMeterType.MANUAL
    assert config.sample_count == 1
    assert config.ct_bri_steps == 15
    assert config.ct_mired_steps == 50
    assert config.bri_bri_steps == 3


def test_cli_environment_preserves_value_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWER_METER", PowerMeterType.HASS)
    monkeypatch.setenv("MIN_BRIGHTNESS", "0")
    monkeypatch.setenv("MAX_SAT", "999")
    monkeypatch.setenv("HS_BRI_PRECISION", "2")
    monkeypatch.setenv("MEASURE_TIME_EFFECT", "12")
    monkeypatch.setenv("MEASURE_TIME_EFFECT_MIN", "30")
    monkeypatch.setenv("MEASURE_TIME_EFFECT_CONVERGENCE_WINDOW", "20")
    monkeypatch.setenv("MEASURE_TIME_EFFECT_CONVERGENCE_REL", "2.5")
    monkeypatch.setenv("SELECTED_MEASURE_TYPE", "Average")

    config = CliEnvironment()

    assert config.min_brightness == 1
    assert config.max_sat == 255
    assert config.hs_bri_precision == 2
    assert config.hs_bri_steps == 16
    assert config.measure_time_effect_min == 12
    assert config.measure_time_effect_convergence_window == 12
    assert config.measure_time_effect_convergence_rel == pytest.approx(0.025)
    assert config.selected_measure_type == MeasureType.AVERAGE


def test_cli_environment_preserves_named_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    assert CliEnvironment().log_level == "DEBUG"


@pytest.mark.parametrize("name", _ENV_BACKED_LIMIT_FIELDS)
def test_cli_environment_clamps_env_values_to_parameter_limits(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    minimum, maximum = PARAMETER_LIMITS[name]
    if name == "measure_time_effect_min":
        # Otherwise the relative cap min(value, measure_time_effect) hides the table clamp.
        monkeypatch.setenv("MEASURE_TIME_EFFECT", str(int(PARAMETER_LIMITS["measure_time_effect"][1])))
    config = CliEnvironment()

    monkeypatch.setenv(name.upper(), str(int(maximum) + 1))
    assert getattr(config, name) == maximum

    monkeypatch.setenv(name.upper(), str(int(minimum) - 1))
    assert getattr(config, name) == minimum


def test_cli_environment_parses_witness_meters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWER_METER", PowerMeterType.OCR)
    monkeypatch.setenv("WITNESS_METERS", " shelly, hass ")
    monkeypatch.setenv("WITNESS_SHELLY_OFFSET_W", "2.45")
    monkeypatch.setenv("WITNESS_SHELLY_TOLERANCE_W", "0.3")
    monkeypatch.setenv("WITNESS_HASS_TOLERANCE_PCT", "5")
    monkeypatch.setenv("WITNESS_HASS_REQUIRED", "false")

    config = CliEnvironment()

    assert config.witness_meters == [PowerMeterType.SHELLY, PowerMeterType.HASS]
    assert uses_power_meter(config, PowerMeterType.OCR)
    assert uses_power_meter(config, PowerMeterType.SHELLY)
    assert not uses_power_meter(config, PowerMeterType.KASA)
    assert config.witness_offset_w(PowerMeterType.SHELLY) == 2.45
    assert config.witness_tolerance_w(PowerMeterType.SHELLY) == 0.3
    assert config.witness_tolerance_pct(PowerMeterType.SHELLY) == 2.0
    assert config.witness_required(PowerMeterType.SHELLY) is True
    assert config.witness_offset_w(PowerMeterType.HASS) == 0.0
    assert config.witness_tolerance_w(PowerMeterType.HASS) == 0.5
    assert config.witness_tolerance_pct(PowerMeterType.HASS) == 5.0
    assert config.witness_required(PowerMeterType.HASS) is False


def test_cli_environment_has_no_witnesses_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WITNESS_METERS", raising=False)
    assert CliEnvironment().witness_meters == []
    monkeypatch.setenv("WITNESS_METERS", "")
    assert CliEnvironment().witness_meters == []


@pytest.mark.parametrize(
    "value, message",
    [
        ("nope", "unknown power meter type 'nope'"),
        ("composite", "composite cannot be used as a witness"),
        ("manual", "manual cannot be used as a witness"),
        ("shelly", "shelly is already the primary meter"),
        ("hass,hass", "hass is listed twice"),
    ],
)
def test_cli_environment_rejects_invalid_witnesses(value: str, message: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POWER_METER", PowerMeterType.SHELLY)
    monkeypatch.setenv("WITNESS_METERS", value)

    with pytest.raises(ValueError, match=message):
        _ = CliEnvironment().witness_meters


def test_cli_environment_hass_max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HASS_MAX_AGE_SECONDS", raising=False)
    assert CliEnvironment().hass_max_age_seconds is None
    monkeypatch.setenv("HASS_MAX_AGE_SECONDS", "45")
    assert CliEnvironment().hass_max_age_seconds == 45.0
    monkeypatch.setenv("HASS_MAX_AGE_SECONDS", "0")
    assert CliEnvironment().hass_max_age_seconds is None


def test_cli_environment_ocr_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OCR_SOURCE",
        "OCR_LAYOUT",
        "OCR_PREVIEW_HOST",
        "OCR_PREVIEW_PORT",
        "OCR_WINDOW_SECONDS",
        "OCR_STALE_AFTER_SECONDS",
        "OCR_CROSSCHECK_TOLERANCE_PCT",
    ):
        monkeypatch.delenv(name, raising=False)
    config = CliEnvironment()
    assert config.ocr_source == "0"
    assert config.ocr_layout == "pr10"
    assert config.ocr_preview_host == "127.0.0.1"
    assert config.ocr_preview_port == 8765
    assert config.ocr_window_seconds == 1.5
    assert config.ocr_stale_after_seconds == 5.0
    assert config.ocr_crosscheck_tolerance_pct == 3.0


def test_cli_environment_ocr_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_SOURCE", "http://camera.local:8080/")
    monkeypatch.setenv("OCR_LAYOUT", "pr10")
    monkeypatch.setenv("OCR_PREVIEW_HOST", "192.0.2.5")
    monkeypatch.setenv("OCR_PREVIEW_PORT", "0")
    monkeypatch.setenv("OCR_WINDOW_SECONDS", "2")
    monkeypatch.setenv("OCR_STALE_AFTER_SECONDS", "8")
    monkeypatch.setenv("OCR_CROSSCHECK_TOLERANCE_PCT", "4.5")
    config = CliEnvironment()
    assert config.ocr_source == "http://camera.local:8080/"
    assert config.ocr_preview_host == "192.0.2.5"
    assert config.ocr_preview_port is None  # 0 disables the preview
    assert config.ocr_window_seconds == 2.0
    assert config.ocr_stale_after_seconds == 8.0
    assert config.ocr_crosscheck_tolerance_pct == 4.5


def test_cli_environment_warns_when_clamping(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SAMPLE_COUNT", "500")

    with caplog.at_level(logging.WARNING, logger="measure"):
        assert CliEnvironment().sample_count == 100

    assert "SAMPLE_COUNT=500 is outside the allowed range [1, 100]; using 100" in caplog.text
