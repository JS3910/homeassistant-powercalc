"""Read several power meters at the same instant and let witnesses veto the primary."""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from typing import ClassVar

from measure.powermeter.const import WitnessPosition
from measure.powermeter.errors import PowerMeterError, WitnessDisagreementError
from measure.powermeter.powermeter import PowerMeasurementResult, PowerMeter, PowerMeterDiagnosticSample

_LOGGER = logging.getLogger("measure")


@dataclass(frozen=True)
class Witness:
    """A secondary meter that must agree with the primary for a sample to count.

    ``offset_w`` is subtracted from the witness reading before comparing it to the
    primary. A reading agrees when it is within ``max(tolerance_w,
    tolerance_pct % of the primary)`` after correction. With ``required`` false a
    witness that fails to read is logged and skipped instead of failing the sample.

    ``position`` decides whether ``offset_w`` *also* corrects the primary's recorded
    reading, not just the comparison — see ``WitnessPosition`` and ``spec.WitnessSpec``.
    Only ``AFTER_PRIMARY`` does; the caller building this is responsible for having
    already applied the sign convention that implies (``offset_w`` negative there).
    """

    name: str
    meter: PowerMeter
    position: WitnessPosition = WitnessPosition.NONE
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
    voltage: float | None = None
    current: float | None = None
    power_factor: float | None = None


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
        # Purely a logging label the caller can set (e.g. MeasureUtil toggles it between
        # "settling" while polling for a plateau and "accepted" for a sample that actually
        # counts toward the recorded measurement) -- get_power()'s own behavior never
        # depends on it. Defaults to "reading" for callers that never set it.
        self.log_context: str = "reading"

    @property
    def primary(self) -> PowerMeter:
        return self._primary

    @property
    def witnesses(self) -> tuple[Witness, ...]:
        return self._witnesses

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        primary_future = self._executor.submit(self._primary.get_power, include_voltage)
        # include_voltage=True even though only the correction math below needs .power --
        # the raw-samples diagnostics writer wants whatever V/I/PF a witness can report
        # too, and it's cheap: the meters that support it already compute it per reading.
        witness_futures = [self._executor.submit(witness.meter.get_power, True) for witness in self._witnesses]

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
                    voltage=result.voltage,
                    current=result.current,
                    power_factor=result.power_factor,
                ),
            )

        reading = CompositeReading(primary=primary, witnesses=tuple(readings))
        self.last_reading = reading

        # Only an AFTER_PRIMARY witness (between the primary and the device) means the
        # primary is also seeing the witness's own self-consumption as if it were part
        # of the device's draw — the only case that needs correcting here. NONE and
        # BEFORE_PRIMARY never touch the primary, regardless of offset_w's sign: NONE's
        # offset is the witness's own calibration bias (irrelevant to the primary), and
        # BEFORE_PRIMARY's is already fully accounted for by correcting the witness
        # above, not the primary. Deliberately not inferred from offset_w's sign alone —
        # a NONE witness may legitimately have a negative offset that must never reach
        # here.
        correction = sum(
            witness.offset_w for witness in self._witnesses if witness.position == WitnessPosition.AFTER_PRIMARY
        )
        compensated_primary = primary.power + correction if correction else primary.power
        # log_context used to be shown twice on this one line -- once as a raw label up
        # front ("Meters (settling): ...") and again, capitalized, at the end via
        # _describe -- with no visible difference between the two. Only the trailing,
        # human-readable word is kept now; see _describe for what it actually says.
        _LOGGER.info("Meters: %s", self._describe(reading))

        disagreeing = [r for r in readings if not r.agrees]
        if disagreeing:
            raise WitnessDisagreementError(
                f"{self._primary_name} read {primary.power:.3f} W but "
                + "; ".join(self._describe_witness(r) for r in disagreeing),
            )
        return primary._replace(power=compensated_primary) if correction else primary

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

    def recover(self) -> None:
        for meter in (self._primary, *(witness.meter for witness in self._witnesses)):
            try:
                meter.recover()
            except Exception as error:  # noqa: BLE001 - every meter must get its chance to reconnect
                _LOGGER.warning("Could not recover %s: %s", meter, error)

    def _describe(self, reading: CompositeReading) -> str:
        """The values this poll actually compared, not the raw sensor outputs.

        Agreement is ``corrected witness`` vs ``raw primary``. The first number is that
        primary; each parenthetical is that witness after its offset. A later
        ``AFTER_PRIMARY`` subtraction from the recorded primary is a different step and
        shows up on the ``Measurement:`` line, not here.
        """
        witness_values = ", ".join(
            f"{w.name} {w.corrected:.2f} W" if w.corrected is not None else f"{w.name} error" for w in reading.witnesses
        )
        deltas = ", ".join(self._describe_delta(w, prefix=len(reading.witnesses) > 1) for w in reading.witnesses)
        return f"{reading.primary.power:.2f} W ({witness_values}), {deltas}, {self._log_context_label()}"

    # Maps the internal log_context labels the light runner's settle wait uses (see
    # MeasureUtil.wait_for_plateau and LightRunner._settle) onto the three words that
    # should actually end this line, so "did it settle or did it just give up" is
    # legible without cross-referencing code: still polling ("Settling"), held flat
    # ("Stable"), or gave up at the configured cap ("Reached max settle time"). Any other
    # caller's label (e.g. "reading", "accepted, sample 2/3" from a runner with no settle
    # step of its own) is shown as before, title-cased.
    _LOG_CONTEXT_LABELS: ClassVar[dict[str, str]] = {
        "settling": "Settling",
        "settled": "Stable",
        "settle cap hit": "Reached max settle time",
    }

    def _log_context_label(self) -> str:
        return self._LOG_CONTEXT_LABELS.get(self.log_context, self.log_context.capitalize())

    @staticmethod
    def _describe_delta(reading: WitnessReading, *, prefix: bool) -> str:
        label = f"{reading.name} " if prefix else ""
        if reading.power is None:
            return f"{label}error ({reading.error})"
        assert reading.deviation is not None
        return f"{label}\u0394 {abs(reading.deviation):.2f} W {'OK' if reading.agrees else 'REJECTED'}"

    @staticmethod
    def _describe_witness(reading: WitnessReading) -> str:
        if reading.power is None:
            return f"{reading.name}=error ({reading.error})"
        assert reading.corrected is not None
        assert reading.deviation is not None
        # "deviation" is corrected witness minus primary: negative means the witness
        # (after its own offset correction) read lower than the primary, positive higher.
        raw = f"raw {reading.power:.3f} W, " if abs(reading.power - reading.corrected) > 1e-9 else ""
        return (
            f"{reading.name}={reading.corrected:.3f} W ({raw}"
            f"deviation {reading.deviation:+.3f} W, {'ok' if reading.agrees else 'DISAGREES'})"
        )
