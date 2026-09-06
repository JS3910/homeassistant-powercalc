from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import NamedTuple


class PowerMeter(ABC):
    @abstractmethod
    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        """Get a power measurement from the meter. Optionally include voltage readings."""

    @abstractmethod
    def has_voltage_support(self) -> bool:
        """Returns bool depending on the powermeter capabilities to act as a voltmeter."""

    def diagnostic_sample(self) -> PowerMeterDiagnosticSample:
        """Return one raw-enough sample for connection and quality diagnostics."""

        reading = self.get_power(include_voltage=False)
        return PowerMeterDiagnosticSample(
            power=reading.power,
            raw_value=str(reading.power),
            reported_at=reading.updated,
        )

    def close(self) -> None:  # noqa: B027 - optional lifecycle hook
        """Release any background thread, socket, or connection this meter is holding.

        Most adapters are stateless HTTP/HA calls with nothing to release, hence the
        no-op default. OCR and any composite wrapping it are the exception: they own a
        capture thread and an embedded preview HTTP server bound to a real port, which
        must be freed before the same spec can be built again -- otherwise the next
        attempt (a retry, or the app's own active-light preflight check followed by the
        real run) fails with "Address already in use" instead of a fresh preview.
        """


class PowerMeasurementResult(NamedTuple):
    power: float
    updated: float
    voltage: float | None = None
    # Only the OCR meter populates these today, read straight off the display alongside
    # power; every other adapter leaves them None rather than guessing. Additive fields --
    # nothing reads them yet except the raw-samples diagnostics writer (see
    # measure.runner.raw_sample_writer), so a meter that doesn't set them changes nothing.
    current: float | None = None
    power_factor: float | None = None


@dataclass(frozen=True)
class PowerMeterDiagnosticSample:
    power: float
    raw_value: str
    reported_at: float
