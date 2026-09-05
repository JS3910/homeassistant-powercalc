from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from measure.powermeter.const import OwonOwh98xxChannelType, PowerMeterType

POWER_ENTITY_PATTERN = r"^sensor\.[a-z0-9_]+$"
VOLTAGE_ENTITY_PATTERN = r"^sensor\.[a-z0-9_]+$"


class _PowerMeterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DummyPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.DUMMY] = PowerMeterType.DUMMY


class HassPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.HASS] = PowerMeterType.HASS
    entity_id: str = Field(pattern=POWER_ENTITY_PATTERN)
    voltage_entity_id: str | None = Field(default=None, pattern=VOLTAGE_ENTITY_PATTERN)
    call_update_entity: bool = False
    max_age_seconds: float | None = Field(default=None, gt=0)
    """Reject a reading whose ``last_reported`` is older than this; ``None`` disables the check."""

    @field_validator("voltage_entity_id", mode="before")
    @classmethod
    def empty_voltage_entity_is_none(cls, value: str | None) -> str | None:
        return value or None


class KasaPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.KASA] = PowerMeterType.KASA
    device_ip: str


class ManualPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.MANUAL] = PowerMeterType.MANUAL


class MyStromPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.MYSTROM] = PowerMeterType.MYSTROM
    device_ip: str


class OcrPowerMeterSpec(_PowerMeterSpec):
    """Read a physical meter's display through a camera.

    ``source`` is a local camera index (``"0"``), a stream URL (an MJPEG stream from an
    ESPHome camera, for example) or a video file path. ``preview_port`` ``None`` disables
    the embedded browser preview.
    """

    type: Literal[PowerMeterType.OCR] = PowerMeterType.OCR
    source: str = "0"
    layout: str = "pr10"
    preview_host: str = "127.0.0.1"
    preview_port: int | None = Field(default=8765, ge=1, le=65535)
    window_seconds: float = Field(default=1.5, gt=0)
    stale_after_seconds: float = Field(default=5.0, gt=0)
    crosscheck_tolerance_pct: float = Field(default=3.0, ge=0)
    min_current_for_crosscheck: float = Field(default=0.02, ge=0)


class ShellyPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.SHELLY] = PowerMeterType.SHELLY
    device_ip: str
    username: str = Field(default="admin", min_length=1, max_length=50)
    timeout: int = 5


class TasmotaPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.TASMOTA] = PowerMeterType.TASMOTA
    device_ip: str


class TuyaPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.TUYA] = PowerMeterType.TUYA
    device_id: str
    device_ip: str
    version: str = "3.3"


class OwonOwh98xxPowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.OWON_OWH98XX] = PowerMeterType.OWON_OWH98XX
    port: str
    baudrate: int
    timeout: float = 5.0
    channel: OwonOwh98xxChannelType


SinglePowerMeterSpec = Annotated[
    DummyPowerMeterSpec
    | HassPowerMeterSpec
    | KasaPowerMeterSpec
    | ManualPowerMeterSpec
    | MyStromPowerMeterSpec
    | OcrPowerMeterSpec
    | ShellyPowerMeterSpec
    | TasmotaPowerMeterSpec
    | TuyaPowerMeterSpec
    | OwonOwh98xxPowerMeterSpec,
    Field(discriminator="type"),
]
"""Every meter that reads one physical device. Composites are built from these."""


class WitnessSpec(_PowerMeterSpec):
    """A secondary meter read together with the primary; it must agree or the sample is retried.

    ``offset_w`` is subtracted from the witness reading first, for a witness wired
    upstream of the primary that therefore also measures the primary's own draw.
    Agreement means the corrected reading is within ``max(tolerance_w, tolerance_pct)``
    of the primary. A ``required`` witness that fails to read fails the sample; an
    optional one is logged and skipped.
    """

    meter: SinglePowerMeterSpec
    offset_w: float = 0.0
    tolerance_w: float = Field(default=0.5, ge=0)
    tolerance_pct: float = Field(default=2.0, ge=0)
    required: bool = True


class CompositePowerMeterSpec(_PowerMeterSpec):
    type: Literal[PowerMeterType.COMPOSITE] = PowerMeterType.COMPOSITE
    primary: SinglePowerMeterSpec
    witnesses: list[WitnessSpec] = Field(min_length=1)


PowerMeterSpec = Annotated[
    CompositePowerMeterSpec
    | DummyPowerMeterSpec
    | HassPowerMeterSpec
    | KasaPowerMeterSpec
    | ManualPowerMeterSpec
    | MyStromPowerMeterSpec
    | OcrPowerMeterSpec
    | ShellyPowerMeterSpec
    | TasmotaPowerMeterSpec
    | TuyaPowerMeterSpec
    | OwonOwh98xxPowerMeterSpec,
    Field(discriminator="type"),
]
