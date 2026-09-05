from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from measure.const import PARAMETER_LIMITS
from measure.powermeter.const import OwonOwh98xxChannelType, PowerMeterType
from measure.powermeter.spec import POWER_ENTITY_PATTERN, VOLTAGE_ENTITY_PATTERN
from measure.tuning import MeasurementParameters

_DEFAULTS = MeasurementParameters()


def _bounded_field(name: str, default: float) -> Any:  # noqa: ANN401  # typed as Any so assignments match the field's type, like pydantic's Field()
    minimum, maximum = PARAMETER_LIMITS[name]
    return Field(default=default, ge=minimum, le=maximum)


class WitnessMeterSettings(BaseModel):
    """One meter's settings-level configuration: a witness's, or (via
    ``AppPreferences.primary_meter_draft``) the primary's, converted to the same shape.

    Every field except ``type`` is optional, unlike the strict ``SinglePowerMeterSpec``
    variants, so an incomplete draft — a witness added but not yet addressed, for
    example — can still be persisted and edited. Conversion into a validated
    ``SinglePowerMeterSpec`` happens only when a spec is actually needed, in
    ``_single_meter_spec`` (``ha_app/api.py``), which raises ``PowerMeterError`` with a
    field-specific message for whatever is still missing.
    """

    model_config = ConfigDict(extra="ignore")

    type: PowerMeterType = PowerMeterType.SHELLY
    entity_id: str | None = Field(default=None, pattern=POWER_ENTITY_PATTERN)
    voltage_entity_id: str | None = Field(default=None, pattern=VOLTAGE_ENTITY_PATTERN)
    max_age_seconds: float | None = Field(default=None, gt=0)
    device_ip: str | None = Field(default=None, max_length=255)
    username: str = Field(default="admin", min_length=1, max_length=50)
    device_id: str | None = Field(default=None, max_length=255)
    version: str = Field(default="3.3", max_length=20)
    port: str | None = Field(default=None, max_length=255)
    baudrate: int | None = None
    timeout: float = Field(default=5.0, gt=0)
    channel: OwonOwh98xxChannelType | None = None
    source: str = Field(default="0", max_length=500)
    layout: str = Field(default="pr10", max_length=100)
    preview_host: str = Field(default="127.0.0.1", max_length=255)
    preview_port: int | None = Field(default=8765, ge=1, le=65535)
    window_seconds: float = Field(default=1.5, gt=0)
    stale_after_seconds: float = Field(default=5.0, gt=0)
    crosscheck_tolerance_pct: float = Field(default=3.0, ge=0)
    min_current_for_crosscheck: float = Field(default=0.02, ge=0)


class WitnessSettings(BaseModel):
    """A witness meter to read alongside the primary, in the shape a saved session default uses.

    Mirrors ``measure.powermeter.spec.WitnessSpec`` field-for-field; see that class for what
    each tolerance and ``required`` mean.
    """

    model_config = ConfigDict(extra="ignore")

    meter: WitnessMeterSettings
    offset_w: float = 0.0
    tolerance_w: float = Field(default=0.5, ge=0)
    tolerance_pct: float = Field(default=2.0, ge=0)
    required: bool = True


class AppMeasurementDefaults(BaseModel):
    """Reusable measurement behavior copied into each new request."""

    model_config = ConfigDict(extra="ignore")

    sleep_time: float = _bounded_field("sleep_time", _DEFAULTS.sleep_time)
    sample_count: int = _bounded_field("sample_count", _DEFAULTS.sample_count)
    sleep_time_sample: int = _bounded_field("sleep_time_sample", _DEFAULTS.sleep_time_sample)
    max_retries: int = _bounded_field("max_retries", _DEFAULTS.max_retries)
    max_nudges: int = _bounded_field("max_nudges", _DEFAULTS.max_nudges)


class AppPreferences(BaseModel):
    """Persisted defaults for new sessions, tolerant of unknown settings keys."""

    model_config = ConfigDict(extra="ignore")

    default_power_entity_id: str | None = Field(default=None, pattern=POWER_ENTITY_PATTERN)
    default_measure_device: str | None = Field(default=None, max_length=200)
    default_measure_device_firmware: str | None = Field(default=None, max_length=200)
    default_contributor_name: str | None = Field(default=None, max_length=200)
    default_contributor_github: str | None = Field(default=None, max_length=100)
    default_contributor_email: str | None = Field(default=None, max_length=200)
    power_meter: PowerMeterType = PowerMeterType.HASS
    shelly_ip: str | None = Field(default=None, max_length=255)
    shelly_username: str = Field(default="admin", min_length=1, max_length=50)
    kasa_ip: str | None = Field(default=None, max_length=255)
    # Added alongside the original four fields above (never renamed or removed, so an
    # existing settings.json from before witness support loads unchanged): one flat
    # optional field per additional primary meter type, plus the witness list, which
    # defaults to empty — meaning "no witnesses, behave exactly as before" for every
    # settings.json that predates this.
    hass_max_age_seconds: float | None = Field(default=None, gt=0)
    mystrom_ip: str | None = Field(default=None, max_length=255)
    tasmota_ip: str | None = Field(default=None, max_length=255)
    tuya_device_id: str | None = Field(default=None, max_length=255)
    tuya_device_ip: str | None = Field(default=None, max_length=255)
    tuya_version: str = Field(default="3.3", max_length=20)
    owon_port: str | None = Field(default=None, max_length=255)
    owon_baudrate: int | None = None
    owon_timeout: float = Field(default=5.0, gt=0)
    owon_channel: OwonOwh98xxChannelType | None = None
    ocr_source: str = Field(default="0", max_length=500)
    ocr_layout: str = Field(default="pr10", max_length=100)
    ocr_preview_host: str = Field(default="127.0.0.1", max_length=255)
    ocr_preview_port: int | None = Field(default=8765, ge=1, le=65535)
    ocr_window_seconds: float = Field(default=1.5, gt=0)
    ocr_stale_after_seconds: float = Field(default=5.0, gt=0)
    ocr_crosscheck_tolerance_pct: float = Field(default=3.0, ge=0)
    ocr_min_current_for_crosscheck: float = Field(default=0.02, ge=0)
    witnesses: list[WitnessSettings] = Field(default_factory=list)
    fast_test_mode: bool = False
    measurement_defaults: AppMeasurementDefaults = Field(default_factory=AppMeasurementDefaults)

    def primary_meter_draft(self) -> WitnessMeterSettings:
        """The configured primary meter, converted to the same draft shape a witness uses.

        Lets ``_single_meter_spec`` (``ha_app/api.py``) build both the primary and every
        witness through one conversion path, even though the primary's fields stay flat
        (for backward compatibility) while witnesses are a list of nested drafts.
        """
        device_ip_by_type: dict[PowerMeterType, str | None] = {
            PowerMeterType.SHELLY: self.shelly_ip,
            PowerMeterType.KASA: self.kasa_ip,
            PowerMeterType.MYSTROM: self.mystrom_ip,
            PowerMeterType.TASMOTA: self.tasmota_ip,
            PowerMeterType.TUYA: self.tuya_device_ip,
        }
        return WitnessMeterSettings(
            type=self.power_meter,
            entity_id=self.default_power_entity_id,
            max_age_seconds=self.hass_max_age_seconds,
            device_ip=device_ip_by_type.get(self.power_meter),
            username=self.shelly_username,
            device_id=self.tuya_device_id,
            version=self.tuya_version,
            port=self.owon_port,
            baudrate=self.owon_baudrate,
            timeout=self.owon_timeout,
            channel=self.owon_channel,
            source=self.ocr_source,
            layout=self.ocr_layout,
            preview_host=self.ocr_preview_host,
            preview_port=self.ocr_preview_port,
            window_seconds=self.ocr_window_seconds,
            stale_after_seconds=self.ocr_stale_after_seconds,
            crosscheck_tolerance_pct=self.ocr_crosscheck_tolerance_pct,
            min_current_for_crosscheck=self.ocr_min_current_for_crosscheck,
        )


class AppSettingsUpdate(AppPreferences):
    """Settings accepted from the UI, including write-only credential changes."""

    shelly_password: str | None = Field(default=None, max_length=255)
    clear_shelly_password: bool = False

    def preferences(self) -> AppPreferences:
        return AppPreferences.model_validate(self.model_dump())


class AppSettingsResponse(AppPreferences):
    """Public settings state which never returns the Shelly password."""

    shelly_password_configured: bool = False
