import logging
import threading
import time

from measure.powermeter.composite import CompositePowerMeter, Witness
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

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        self.calls.append(include_voltage)
        if self.error is not None:
            raise self.error
        voltage = self.voltage if include_voltage else None
        return PowerMeasurementResult(power=self.power, updated=123.0, voltage=voltage)

    def has_voltage_support(self) -> bool:
        return self.voltage_support


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
    assert witness.calls == [False]
    assert meter.last_reading is not None
    reading = meter.last_reading.witnesses[0]
    assert reading.agrees
    assert reading.corrected == pytest.approx(4.95)
    assert reading.deviation == pytest.approx(0.01)


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
    with pytest.raises(WitnessDisagreementError):
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
    assert "w=error (ApiConnectionError)" in caplog.text
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
