import logging
import threading
import time

from measure.powermeter.composite import CompositePowerMeter, Witness
from measure.powermeter.const import WitnessPosition
from measure.powermeter.errors import ApiConnectionError, WitnessDisagreementError
from measure.powermeter.powermeter import PowerMeasurementResult, PowerMeter, PowerMeterDiagnosticSample
import pytest


class FakeMeter(PowerMeter):
    def __init__(self, power: float, *, voltage: float | None = None, error: Exception | None = None) -> None:
        self.power = power
        self.voltage = voltage
        self.error = error
        self.calls: list[bool] = []
        self.voltage_support = voltage is not None
        self.closed = False
        self.close_error: Exception | None = None
        self.recovered = 0
        self.recover_error: Exception | None = None

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        self.calls.append(include_voltage)
        if self.error is not None:
            raise self.error
        voltage = self.voltage if include_voltage else None
        return PowerMeasurementResult(power=self.power, updated=123.0, voltage=voltage)

    def has_voltage_support(self) -> bool:
        return self.voltage_support

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def recover(self) -> None:
        self.recovered += 1
        if self.recover_error is not None:
            raise self.recover_error


def _composite(primary: PowerMeter, *witnesses: Witness) -> CompositePowerMeter:
    return CompositePowerMeter(primary, witnesses, primary_name="pr10")


def test_requires_a_witness() -> None:
    with pytest.raises(ValueError, match="at least one witness"):
        CompositePowerMeter(FakeMeter(1.0), [])


def test_returns_primary_reading_unchanged_when_witness_agrees() -> None:
    primary = FakeMeter(4.94, voltage=232.9)
    witness = FakeMeter(7.40)
    meter = _composite(primary, Witness(name="shelly", meter=witness, offset_w=2.45))

    result = meter.get_power(include_voltage=True)

    assert result == PowerMeasurementResult(power=4.94, updated=123.0, voltage=232.9)
    assert primary.calls == [True]
    assert witness.calls == [True]
    assert meter.last_reading is not None
    reading = meter.last_reading.witnesses[0]
    assert reading.agrees
    assert reading.corrected == pytest.approx(4.95)
    assert reading.deviation == pytest.approx(0.01)


def test_after_primary_witness_corrects_the_recorded_primary_reading() -> None:
    """The primary is also seeing the witness's own self-consumption, so it must be subtracted.

    Reproduces the real bug found 2026-09-06: Shelly (primary, upstream) reads the light
    plus the PR10 witness's own ~2.4 W draw; PR10 (downstream) reads the light alone. The
    negative ``offset_w`` an ``AFTER_PRIMARY`` witness takes (a positive magnitude, sign
    applied by whoever builds this, matching ``WitnessSpec.signed_offset_w``) must come off
    the *recorded* value, not just the agreement comparison.
    """

    primary = FakeMeter(2.8)
    witness = FakeMeter(0.35)
    ocr_witness = Witness(name="ocr", meter=witness, position=WitnessPosition.AFTER_PRIMARY, offset_w=-2.45)
    meter = _composite(primary, ocr_witness)

    result = meter.get_power()

    assert result.power == pytest.approx(2.8 - 2.45)


def test_log_line_shows_the_compared_values_not_the_raw_witness(caplog: pytest.LogCaptureFixture) -> None:
    """The compact line is the pair that decide agree/disagree: raw primary vs
    offset-corrected witness. A BEFORE_PRIMARY Shelly still reads ~3 W on the wire;
    after its 2.4 W offset the comparison uses 0.70 W, and that is what the line must
    print — not the raw 3.10 W, which would make Δ 0.25 look invented.
    """
    meter = _composite(
        FakeMeter(0.45),
        Witness(name="shelly", meter=FakeMeter(3.10), position=WitnessPosition.BEFORE_PRIMARY, offset_w=2.40),
    )

    with caplog.at_level(logging.INFO, logger="measure"):
        result = meter.get_power()

    assert result.power == pytest.approx(0.45)
    assert "0.45 W (shelly 0.70 W), \u0394 0.25 W OK, Reading" in caplog.text
    assert "shelly 3.10 W" not in caplog.text


def test_log_line_keeps_after_primary_comparison_values_even_when_the_recording_is_subtracted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AFTER_PRIMARY still records primary minus the witness's own draw, but the Meters
    line is the comparison (raw primary vs corrected witness), not that recorded value.
    """
    meter = _composite(
        FakeMeter(3.0),
        Witness(name="ocr", meter=FakeMeter(0.53), position=WitnessPosition.AFTER_PRIMARY, offset_w=-2.47),
    )

    with caplog.at_level(logging.INFO, logger="measure"):
        result = meter.get_power()

    assert result.power == pytest.approx(0.53)
    assert "3.00 W (ocr 3.00 W), \u0394 0.00 W OK, Reading" in caplog.text


def test_log_line_reflects_the_log_context_set_by_the_caller(caplog: pytest.LogCaptureFixture) -> None:
    meter = _composite(FakeMeter(3.0), Witness(name="ocr", meter=FakeMeter(3.0)))
    meter.log_context = "settling"

    with caplog.at_level(logging.INFO, logger="measure"):
        meter.get_power()

    assert caplog.text.strip().endswith("Settling")


@pytest.mark.parametrize(
    "log_context, expected_word",
    [
        ("settling", "Settling"),
        ("settled", "Stable"),
        ("settle cap hit", "Reached max settle time"),
    ],
)
def test_log_line_maps_settle_outcomes_to_their_display_words_without_a_redundant_prefix(
    caplog: pytest.LogCaptureFixture,
    log_context: str,
    expected_word: str,
) -> None:
    """The line used to show log_context twice -- once as a raw label up front
    ("Meters (settle cap hit): ...") and again, merely capitalized, at the end -- with no
    real difference between the two. Now it appears once, at the end, mapped onto the
    three words a settle-outcome line should end with: "Settling", "Stable", or "Reached
    max settle time" (LightRunner._settle sets "settled"/"settle cap hit"; wait_for_plateau
    sets "settling").
    """
    meter = _composite(FakeMeter(3.0), Witness(name="ocr", meter=FakeMeter(3.0)))
    meter.log_context = log_context

    with caplog.at_level(logging.INFO, logger="measure"):
        meter.get_power()

    line = next(record.message for record in caplog.records if record.message.startswith("Meters"))
    assert line == f"Meters: 3.00 W (ocr 3.00 W), \u0394 0.00 W OK, {expected_word}"
    assert not line.startswith(f"Meters ({log_context})")


def test_before_primary_witness_never_corrects_the_recorded_primary_reading() -> None:
    """The primary already reads the device correctly; only the witness needed correcting."""

    primary = FakeMeter(4.94)
    witness = FakeMeter(7.40)
    shelly_witness = Witness(name="shelly", meter=witness, position=WitnessPosition.BEFORE_PRIMARY, offset_w=2.45)
    meter = _composite(primary, shelly_witness)

    result = meter.get_power()

    assert result.power == pytest.approx(4.94)


def test_none_position_witness_never_corrects_the_primary_even_with_a_negative_offset() -> None:
    """A NONE witness's offset is its own calibration bias and must never reach the primary,
    regardless of sign — this is the case a naive "negative offset corrects the primary"
    rule would get wrong."""

    primary = FakeMeter(4.94)
    witness = FakeMeter(4.50)
    meter = _composite(primary, Witness(name="clamp", meter=witness, position=WitnessPosition.NONE, offset_w=-0.44))

    result = meter.get_power()

    assert result.power == pytest.approx(4.94)


def test_multiple_after_primary_witnesses_stack_their_corrections() -> None:
    # Each witness's corrected reading (power - offset_w) must itself agree with the raw
    # primary (10.0) to avoid a WitnessDisagreementError, independent of the correction
    # this test is actually checking: 9.0 - (-1.0) = 10.0, and 8.5 - (-1.5) = 10.0.
    primary = FakeMeter(10.0)
    meter = _composite(
        primary,
        Witness(name="a", meter=FakeMeter(9.0), position=WitnessPosition.AFTER_PRIMARY, offset_w=-1.0),
        Witness(name="b", meter=FakeMeter(8.5), position=WitnessPosition.AFTER_PRIMARY, offset_w=-1.5),
    )

    result = meter.get_power()

    assert result.power == pytest.approx(10.0 - 1.0 - 1.5)


def test_absolute_tolerance_applies_at_low_power() -> None:
    meter = _composite(FakeMeter(1.0), Witness(name="w", meter=FakeMeter(1.49), tolerance_w=0.5, tolerance_pct=2.0))
    meter.get_power()

    meter = _composite(FakeMeter(1.0), Witness(name="w", meter=FakeMeter(1.51), tolerance_w=0.5, tolerance_pct=2.0))
    with pytest.raises(WitnessDisagreementError, match=r"pr10 read 1.000 W but w=1.510 W .*DISAGREES"):
        meter.get_power()


def test_relative_tolerance_applies_at_high_power() -> None:
    witness = Witness(name="w", meter=FakeMeter(1204.0), tolerance_w=0.5, tolerance_pct=2.0)
    _composite(FakeMeter(1181.0), witness).get_power()

    witness = Witness(name="w", meter=FakeMeter(1205.0), tolerance_w=0.5, tolerance_pct=2.0)
    meter = _composite(FakeMeter(1181.0), witness)
    with pytest.raises(WitnessDisagreementError):
        meter.get_power()


def test_dropped_decimal_on_primary_is_rejected() -> None:
    # "406.7" read as 4067 by the primary; the witness sees the real load.
    meter = _composite(FakeMeter(4067.0), Witness(name="shelly", meter=FakeMeter(409.2), offset_w=2.45))
    with pytest.raises(WitnessDisagreementError, match=r"shelly=406\.750 W \(raw 409\.200 W"):
        meter.get_power()


def test_required_witness_failure_fails_the_sample() -> None:
    meter = _composite(FakeMeter(5.0), Witness(name="w", meter=FakeMeter(0.0, error=ApiConnectionError("timeout"))))
    with pytest.raises(WitnessDisagreementError, match=r"w=error \(timeout\)"):
        meter.get_power()


def test_optional_witness_failure_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    meter = _composite(
        FakeMeter(5.0),
        Witness(name="w", meter=FakeMeter(0.0, error=ApiConnectionError()), required=False),
    )
    with caplog.at_level(logging.INFO, logger="measure"):
        result = meter.get_power()

    assert result.power == 5.0
    assert "error (ApiConnectionError)" in caplog.text
    assert meter.last_reading is not None
    assert meter.last_reading.witnesses[0].agrees


def test_primary_failure_propagates() -> None:
    meter = _composite(FakeMeter(0.0, error=ApiConnectionError("down")), Witness(name="w", meter=FakeMeter(1.0)))
    with pytest.raises(ApiConnectionError, match="down"):
        meter.get_power()


def test_all_witnesses_are_reported_when_several_disagree() -> None:
    meter = _composite(
        FakeMeter(10.0),
        Witness(name="a", meter=FakeMeter(10.1)),
        Witness(name="b", meter=FakeMeter(20.0)),
        Witness(name="c", meter=FakeMeter(30.0)),
    )
    with pytest.raises(WitnessDisagreementError) as error:
        meter.get_power()

    message = str(error.value)
    assert "a=" not in message
    assert "b=20.000 W" in message
    assert "c=30.000 W" in message


def test_children_are_read_concurrently() -> None:
    barrier = threading.Barrier(2, timeout=2)

    class BlockingMeter(FakeMeter):
        def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
            # Both reads must be in flight at once for the barrier to release.
            barrier.wait()
            return super().get_power(include_voltage)

    meter = _composite(BlockingMeter(1.0), Witness(name="w", meter=BlockingMeter(1.0)))
    started = time.perf_counter()
    meter.get_power()
    assert time.perf_counter() - started < 2


def test_capabilities_and_diagnostics_delegate_to_primary() -> None:
    primary = FakeMeter(3.0, voltage=230.0)
    meter = _composite(primary, Witness(name="w", meter=FakeMeter(3.0)))

    assert meter.has_voltage_support() is True
    assert meter.primary is primary
    assert len(meter.witnesses) == 1
    assert meter.diagnostic_sample() == PowerMeterDiagnosticSample(power=3.0, raw_value="3.0", reported_at=123.0)
    meter.close()


def test_close_releases_the_primary_and_every_witness() -> None:
    """A composite is the only meter to hold multiple sub-meters, each of which may own
    real resources (the OCR meter's capture thread and preview server, in particular) --
    closing the composite must not leave any of them behind."""
    primary = FakeMeter(3.0)
    witness_a = FakeMeter(3.0)
    witness_b = FakeMeter(3.0)
    meter = _composite(primary, Witness(name="a", meter=witness_a), Witness(name="b", meter=witness_b))

    meter.close()

    assert primary.closed
    assert witness_a.closed
    assert witness_b.closed


def test_close_still_closes_every_meter_when_one_fails_to_close(caplog: pytest.LogCaptureFixture) -> None:
    primary = FakeMeter(3.0)
    primary.close_error = RuntimeError("stuck")
    witness = FakeMeter(3.0)
    meter = _composite(primary, Witness(name="w", meter=witness))

    with caplog.at_level(logging.WARNING):
        meter.close()

    assert witness.closed
    assert "stuck" in caplog.text


def test_recover_forwards_to_the_primary_and_every_witness() -> None:
    primary = FakeMeter(3.0)
    witness_a = FakeMeter(3.0)
    witness_b = FakeMeter(3.0)
    meter = _composite(primary, Witness(name="a", meter=witness_a), Witness(name="b", meter=witness_b))

    meter.recover()

    assert primary.recovered == 1
    assert witness_a.recovered == 1
    assert witness_b.recovered == 1


def test_recover_still_recovers_every_meter_when_one_fails(caplog: pytest.LogCaptureFixture) -> None:
    primary = FakeMeter(3.0)
    primary.recover_error = RuntimeError("stuck")
    witness = FakeMeter(3.0)
    meter = _composite(primary, Witness(name="w", meter=witness))

    with caplog.at_level(logging.WARNING):
        meter.recover()

    assert witness.recovered == 1
    assert "stuck" in caplog.text
