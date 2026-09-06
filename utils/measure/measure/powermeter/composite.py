"""Read several power meters at the same instant and let witnesses veto the primary."""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging

from measure.powermeter.errors import PowerMeterError, WitnessDisagreementError
from measure.powermeter.powermeter import PowerMeasurementResult, PowerMeter, PowerMeterDiagnosticSample

_LOGGER = logging.getLogger("measure")


@dataclass(frozen=True)
class Witness:
    """A secondary meter that must agree with the primary for a sample to count.

    ``offset_w`` is subtracted from the witness reading before comparing, for a
    witness that sits upstream of the primary and therefore also sees the primary's
    own consumption. A reading agrees when it is within ``max(tolerance_w,
    tolerance_pct % of the primary)`` after correction. With ``required`` false a
    witness that fails to read is logged and skipped instead of failing the sample.
    """

    name: str
    meter: PowerMeter
    offset_w: float = 0.0
    tolerance_w: float = 0.5
    tolerance_pct: float = 2.0
    required: bool = True


@dataclass(frozen=True)
class WitnessReading:
    name: str
    power: float | None
    corrected: float | None
    deviation: float | None
    agrees: bool
    error: str | None = None


@dataclass(frozen=True)
class CompositeReading:
    primary: PowerMeasurementResult
    witnesses: tuple[WitnessReading, ...]


class CompositePowerMeter(PowerMeter):
    """Primary meter whose readings are cross-checked against concurrently read witnesses.

    The primary's ``PowerMeasurementResult`` is returned unchanged, so the runner
    records exactly what it would have recorded with the primary alone; the witnesses
    only decide whether the sample is trusted. Disagreement raises
    ``WitnessDisagreementError``, a ``PowerMeterError``, so the normal retry path
    applies.
    """

    def __init__(self, primary: PowerMeter, witnesses: Sequence[Witness], *, primary_name: str = "primary") -> None:
        if not witnesses:
            raise ValueError("A composite power meter needs at least one witness")
        self._primary = primary
        self._primary_name = primary_name
        self._witnesses = tuple(witnesses)
        self._executor = ThreadPoolExecutor(max_workers=len(self._witnesses) + 1, thread_name_prefix="powermeter")
        self.last_reading: CompositeReading | None = None

    @property
    def primary(self) -> PowerMeter:
        return self._primary

    @property
    def witnesses(self) -> tuple[Witness, ...]:
        return self._witnesses

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        primary_future = self._executor.submit(self._primary.get_power, include_voltage)
        witness_futures = [self._executor.submit(witness.meter.get_power, False) for witness in self._witnesses]

        primary = primary_future.result()
        readings: list[WitnessReading] = []
        for witness, future in zip(self._witnesses, witness_futures, strict=True):
            try:
                result = future.result()
            except PowerMeterError as error:
                readings.append(
                    WitnessReading(
                        name=witness.name,
                        power=None,
                        corrected=None,
                        deviation=None,
                        agrees=not witness.required,
                        error=str(error) or type(error).__name__,
                    ),
                )
                continue
            corrected = result.power - witness.offset_w
            deviation = corrected - primary.power
            allowed = max(witness.tolerance_w, witness.tolerance_pct / 100 * abs(primary.power))
            readings.append(
                WitnessReading(
                    name=witness.name,
                    power=result.power,
                    corrected=corrected,
                    deviation=deviation,
                    agrees=abs(deviation) <= allowed,
                ),
            )

        reading = CompositeReading(primary=primary, witnesses=tuple(readings))
        self.last_reading = reading
        _LOGGER.info("Meters: %s", self._describe(reading))

        disagreeing = [r for r in readings if not r.agrees]
        if disagreeing:
            raise WitnessDisagreementError(
                f"{self._primary_name} read {primary.power:.3f} W but "
                + "; ".join(self._describe_witness(r) for r in disagreeing),
            )
        return primary

    def has_voltage_support(self) -> bool:
        return self._primary.has_voltage_support()

    def diagnostic_sample(self) -> PowerMeterDiagnosticSample:
        return self._primary.diagnostic_sample()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        for meter in (self._primary, *(witness.meter for witness in self._witnesses)):
            try:
                meter.close()
            except Exception as error:  # noqa: BLE001 - every meter must get its chance to release resources
                _LOGGER.warning("Could not close %s: %s", meter, error)

    def _describe(self, reading: CompositeReading) -> str:
        parts = [f"{self._primary_name}={reading.primary.power:.3f} W"]
        parts.extend(self._describe_witness(witness) for witness in reading.witnesses)
        return ", ".join(parts)

    @staticmethod
    def _describe_witness(reading: WitnessReading) -> str:
        if reading.power is None:
            return f"{reading.name}=error ({reading.error})"
        assert reading.corrected is not None
        assert reading.deviation is not None
        return (
            f"{reading.name}={reading.power:.3f} W (corrected {reading.corrected:.3f} W, "
            f"{reading.deviation:+.3f} W, {'ok' if reading.agrees else 'DISAGREES'})"
        )
