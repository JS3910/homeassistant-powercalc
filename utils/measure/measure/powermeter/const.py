from enum import StrEnum


class PowerMeterType(StrEnum):
    COMPOSITE = "composite"
    DUMMY = "dummy"
    HASS = "hass"
    KASA = "kasa"
    MANUAL = "manual"
    OCR = "ocr"
    SHELLY = "shelly"
    TASMOTA = "tasmota"
    TUYA = "tuya"
    MYSTROM = "mystrom"
    OWON_OWH98XX = "owh98xx"


class OwonOwh98xxChannelType(StrEnum):
    CHANNEL1 = "1"
    CHANNEL2 = "2"


class WitnessPosition(StrEnum):
    """Where a witness sits relative to the primary in the wiring, if at all.

    Determines whether the witness's own compensation magnitude also corrects the
    *primary's* recorded reading, not just the comparison used to check agreement.
    See ``WitnessSpec`` for the full explanation.
    """

    NONE = "none"
    """The witness is not electrically in line with the primary at all (e.g. a current
    clamp reading the same wire independently). Its compensation magnitude, if any, is a
    fixed calibration bias on the witness itself and never corrects the primary."""

    BEFORE_PRIMARY = "before_primary"
    """The witness sits upstream of the primary (closer to the grid, farther from the
    device). The primary already reads the device correctly on its own; the witness
    additionally sees the primary's own self-consumption, which the compensation
    magnitude corrects for the comparison only."""

    AFTER_PRIMARY = "after_primary"
    """The witness sits downstream of the primary (closer to the device than the primary
    is). The primary additionally sees the witness's own self-consumption, so the
    compensation magnitude also corrects the primary's recorded reading, not just the
    comparison."""


QUESTION_POWERMETER_ENTITY_ID = "powermeter_entity_id"
QUESTION_VOLTAGEMETER_ENTITY_ID = "voltagemeter_entity_id"

SHELLY_INFO_ENDPOINT = "/shelly"
SHELLY_GEN1_STATUS_ENDPOINT = "/status"
SHELLY_RPC_DEVICE_STATUS_ENDPOINT = "/rpc/Shelly.GetStatus"
SHELLY_RPC_SWITCH_STATUS_ENDPOINT = "/rpc/Switch.GetStatus?id={component_id}"
SHELLY_RPC_PM1_STATUS_ENDPOINT = "/rpc/PM1.GetStatus?id={component_id}"
