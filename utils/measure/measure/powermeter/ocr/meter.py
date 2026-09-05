"""Power meter that reads a physical meter's display through a camera."""

from collections import deque
from collections.abc import Callable
import logging
import statistics
import threading
import time
from typing import Any

from measure.powermeter.errors import OutdatedMeasurementError, PowerMeterError
from measure.powermeter.ocr.capture import Frame, FrameProvider
from measure.powermeter.ocr.preview import PreviewServer, annotate
from measure.powermeter.ocr.reader import DisplayReader, DisplayReading
from measure.powermeter.powermeter import PowerMeasurementResult, PowerMeter, PowerMeterDiagnosticSample

_LOGGER = logging.getLogger("measure")


class OcrPowerMeter(PowerMeter):
    """Reads frames continuously on a background thread; ``get_power`` reports the recent median.

    Every frame is parsed and validated (all fields readable, power consistent with
    voltage x current x power factor). ``get_power`` returns the median of the accepted
    readings within ``window_seconds``, which smooths the display's last-digit wobble,
    and refuses with ``OutdatedMeasurementError`` when nothing has been accepted for
    ``stale_after_seconds``, so a covered or moved display makes the run retry instead of
    silently recording the last value. The display is re-located when readings keep
    failing or the picture changes a lot, both signs the meter or camera moved.
    """

    def __init__(
        self,
        frames: FrameProvider,
        reader: DisplayReader,
        *,
        window_seconds: float = 1.5,
        stale_after_seconds: float = 5.0,
        relocate_after_rejections: int = 3,
        relocate_diff_threshold: float = 8.0,
        preview: PreviewServer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._frames = frames
        self._reader = reader
        self._window_seconds = window_seconds
        self._stale_after_seconds = stale_after_seconds
        self._relocate_after_rejections = relocate_after_rejections
        self._relocate_diff_threshold = relocate_diff_threshold
        self._preview = preview
        self._clock = clock
        self._lock = threading.Lock()
        self._accepted: deque[DisplayReading] = deque(maxlen=200)
        self._last: DisplayReading | None = None
        self._consecutive_rejections = 0
        self._counts = {"frames": 0, "accepted": 0, "rejected": 0, "relocations": 0}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ocr-reader", daemon=True)

    def start(self) -> OcrPowerMeter:
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)
        self._frames.close()
        if self._preview is not None:
            self._preview.close()

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        now = self._clock()
        with self._lock:
            recent = [r for r in self._accepted if now - r.timestamp <= self._window_seconds]
            newest = self._accepted[-1] if self._accepted else None
            last = self._last
        if not recent:
            if newest is not None and now - newest.timestamp <= self._stale_after_seconds:
                recent = [newest]
            else:
                raise OutdatedMeasurementError(self._stale_reason(now, newest, last))
        powers = [r.power for r in recent if r.power is not None]
        voltages = [r.voltage for r in recent if r.voltage is not None]
        updated = max(r.timestamp for r in recent)
        return PowerMeasurementResult(
            power=statistics.median(powers),
            updated=updated,
            voltage=statistics.median(voltages) if include_voltage and voltages else None,
        )

    def has_voltage_support(self) -> bool:
        return "voltage" in self._reader.layout.fields

    def diagnostic_sample(self) -> PowerMeterDiagnosticSample:
        with self._lock:
            newest = self._accepted[-1] if self._accepted else None
            last = self._last
        if newest is None or newest.power is None:
            raise PowerMeterError(self._stale_reason(self._clock(), None, last))
        return PowerMeterDiagnosticSample(
            power=newest.power,
            raw_value=newest.raw.get("power", ""),
            reported_at=newest.timestamp,
        )

    def process(self, frame: Frame) -> DisplayReading:
        """Locate if needed, read the frame, and record the outcome. Called per frame by the loop."""
        relocate = (
            self._reader.location is None
            or self._consecutive_rejections >= self._relocate_after_rejections
            or frame.diff_mean > self._relocate_diff_threshold
        )
        if relocate:
            self._counts["relocations"] += 1
            if self._reader.location is not None:
                _LOGGER.info(
                    "Re-locating display (%d rejections in a row, frame change %.1f)",
                    self._consecutive_rejections,
                    frame.diff_mean,
                )
            self._reader.locate(frame.image)
            self._consecutive_rejections = 0
        reading = self._reader.read(frame.image, frame.timestamp)
        with self._lock:
            self._last = reading
            self._counts["frames"] += 1
            if reading.accepted:
                self._accepted.append(reading)
                self._counts["accepted"] += 1
                self._consecutive_rejections = 0
            else:
                self._counts["rejected"] += 1
                self._consecutive_rejections += 1
        if reading.accepted:
            _LOGGER.debug("OCR %s", self._describe(reading))
        else:
            _LOGGER.info("OCR frame rejected: %s", reading.reason)
        if self._preview is not None:
            self._preview.publish(annotate(frame.image, reading, self._frames.fps), self.state())
        return reading

    def state(self) -> dict[str, Any]:
        """Snapshot for the preview page and diagnostics."""
        now = self._clock()
        with self._lock:
            last = self._last
            newest = self._accepted[-1] if self._accepted else None
            recent = [
                r.power for r in self._accepted if now - r.timestamp <= self._window_seconds and r.power is not None
            ]
            counts = dict(self._counts)
        location = self._reader.location
        return {
            **counts,
            "power": statistics.median(recent) if recent else None,
            "frame_age": now - last.timestamp if last is not None else None,
            "accepted_age": now - newest.timestamp if newest is not None else None,
            "fps": self._frames.fps,
            "angle": location.angle if location is not None else None,
            "source_error": getattr(self._frames, "error", None),
            "last": None
            if last is None
            else {
                "accepted": last.accepted,
                "reason": last.reason,
                "power": last.power,
                "voltage": last.voltage,
                "current": last.current,
                "pf": last.pf,
                "raw": last.raw,
                "timestamp": last.timestamp,
            },
        }

    def _run(self) -> None:
        sequence = 0
        while not self._stop.is_set():
            frame = self._frames.wait_for_frame(sequence, timeout=1.0)
            if frame is None:
                continue
            sequence = frame.sequence
            try:
                self.process(frame)
            except Exception:
                _LOGGER.exception("OCR frame processing failed")

    def _stale_reason(self, now: float, newest: DisplayReading | None, last: DisplayReading | None) -> str:
        source_error = getattr(self._frames, "error", None)
        if source_error:
            return f"OCR: {source_error}"
        if last is None:
            return "OCR: no frame processed yet"
        if newest is None:
            return f"OCR: no accepted reading yet; last frame: {last.reason}"
        return f"OCR: last accepted reading is {now - newest.timestamp:.1f}s old; last frame: {last.reason or 'ok'}"

    @staticmethod
    def _describe(reading: DisplayReading) -> str:
        return f"P={reading.power} W V={reading.voltage} I={reading.current} PF={reading.pf}"
