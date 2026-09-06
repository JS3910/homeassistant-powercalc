from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from measure.powermeter.const import OwonOwh98xxChannelType, PowerMeterType, WitnessPosition

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

    ``offset_w`` (always entered as a positive magnitude — how much the witness reads
    high, never a signed correction) is subtracted from the witness reading before
    comparing it to the primary. ``position`` says why the two don't already read the
    same load, and — critically — decides whether ``offset_w`` also corrects the
    *primary's own recorded reading*, not just the comparison:

    - ``NONE``: the witness isn't electrically in line with the primary at all (e.g. a
      current clamp on the same wire). ``offset_w`` is just the witness's own fixed
      calibration bias — it can be negative here, since a miscalibrated meter can read
      either high or low — and it never touches the primary.
    - ``BEFORE_PRIMARY``: the witness sits upstream of the primary (closer to the grid).
      The primary already reads the device correctly by itself; the witness additionally
      sees the primary's own self-consumption, which ``offset_w`` corrects for the
      comparison only.
    - ``AFTER_PRIMARY``: the witness sits downstream of the primary (closer to the
      device). The primary additionally sees the witness's own self-consumption, so
      ``offset_w`` corrects the primary's recorded reading too, not just the comparison —
      otherwise every recorded sample would be biased high by the witness's own draw.
      Confirmed necessary 2026-09-06: a real PR10-witness/Shelly-primary run recorded the
      PR10's ~2.4 W self-consumption as part of the device's power for its whole duration
      before this was caught.

    Agreement means the corrected reading is within ``max(tolerance_w, tolerance_pct)``
    of the primary. A ``required`` witness that fails to read fails the sample; an
    optional one is logged and skipped.
    """

    meter: SinglePowerMeterSpec
    position: WitnessPosition = WitnessPosition.NONE
    offset_w: float = 0.0
    tolerance_w: float = Field(default=0.5, ge=0)
    tolerance_pct: float = Field(default=2.0, ge=0)
    required: bool = True

    @model_validator(mode="after")
    def _offset_sign_matches_position(self) -> Self:
        """Only ``NONE`` (a fixed calibration bias) may be negative.

        ``BEFORE_PRIMARY``/``AFTER_PRIMARY`` derive their sign from the position itself
        (see the class docstring), so a negative value there would silently mean "the
        witness reads low" while ``AFTER_PRIMARY`` unconditionally subtracts it from the
        primary — exactly the kind of sign mixup this field replaced a raw signed offset
        to avoid.
        """

        if self.position != WitnessPosition.NONE and self.offset_w < 0:
            raise ValueError(
                f"offset_w must not be negative when position is {self.position.value!r}; "
                "it is a magnitude, and the sign is implied by the position",
            )
        return self

    @property
    def signed_offset_w(self) -> float:
        """``offset_w`` with the sign ``CompositePowerMeter``'s comparison expects.

        ``NONE`` and ``BEFORE_PRIMARY`` both mean "the witness reads this much high"
        (``offset_w`` as entered, already signed correctly for ``NONE``, and a
        non-negative magnitude for ``BEFORE_PRIMARY``); ``AFTER_PRIMARY`` means "the
        witness reads this much low", i.e. the negative of the entered magnitude.
        """

        return -self.offset_w if self.position == WitnessPosition.AFTER_PRIMARY else self.offset_w


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
