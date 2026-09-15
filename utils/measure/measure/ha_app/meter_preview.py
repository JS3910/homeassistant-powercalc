"""Let the setup/settings UI show a live OCR camera preview before any real measurement
session exists, so the camera can actually be pointed at the meter while configuring it.

This builds the exact same `OcrPowerMeter` (and its `PreviewServer`) a real session would,
via `find_ocr_previews` -- just kept alive under a throwaway id registered into the same
`OcrPreviewRegistry` a session uses, instead of a session id. There is deliberately no
"stop watching" signal from a closed browser tab, so an idle-timeout reaper closes anything
the frontend doesn't keep polling, rather than leaking a camera connection and capture
thread indefinitely.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time
import uuid

from measure.ha_app.ocr_preview import OcrPreviewRegistry, find_ocr_previews
from measure.powermeter.powermeter import PowerMeasurementResult, PowerMeter, PowerMeterDiagnosticSample
from measure.powermeter.spec import PowerMeterSpec, ocr_preview_binds

_LOGGER = logging.getLogger("measure")

# An unpolled preview is closed after this long without a request touching it -- long
# enough that normal polling (the frontend refetches the frame every second or so) never
# trips it, short enough that a closed browser tab's camera connection and capture thread
# don't linger for more than about a minute.
IDLE_TIMEOUT_SECONDS = 60.0
_REAP_INTERVAL_SECONDS = 15.0


@dataclass
class _Entry:
    spec: PowerMeterSpec
    meter: PowerMeter
    last_seen: float


class _UncloseableMeter(PowerMeter):
    """Forwards to a live aiming-preview meter without taking ownership of it.

    Diagnostics and the light-load preflight each ``close()`` the meter they built.
    The setup-page preview must survive those throwaway probes, or the next build
    tries to bind the same preview port and fails with ``Address already in use``.
    """

    def __init__(self, meter: PowerMeter) -> None:
        object.__setattr__(self, "_meter", meter)

    def get_power(self, include_voltage: bool = False) -> PowerMeasurementResult:
        return self._meter.get_power(include_voltage=include_voltage)

    def has_voltage_support(self) -> bool:
        return self._meter.has_voltage_support()

    def diagnostic_sample(self) -> PowerMeterDiagnosticSample:
        return self._meter.diagnostic_sample()

    def close(self) -> None:
        return

    def __getattr__(self, name: str) -> object:
        return getattr(self._meter, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_meter":
            object.__setattr__(self, name, value)
            return
        setattr(self._meter, name, value)


class MeterPreviewService:
    """Builds a throwaway power meter purely to expose whatever OCR preview(s) it has."""

    def __init__(
        self,
        registry: OcrPreviewRegistry,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        start_reaper: bool = True,
    ) -> None:
        self._registry = registry
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        if start_reaper:
            threading.Thread(target=self._reap_loop, name="meter-preview-reaper", daemon=True).start()

    def start(
        self,
        spec: PowerMeterSpec,
        build: Callable[[PowerMeterSpec], PowerMeter],
    ) -> tuple[str | None, tuple[str, ...]]:
        """Build `spec` and register its OCR preview(s), if it has any.

        Returns `(None, ())` -- not an error -- for a meter with no OCR component at all;
        there's simply nothing to preview, same as a session that never configures one.
        The built meter is closed immediately in that case rather than left running.
        """

        with self._lock:
            for preview_id, entry in self._entries.items():
                if entry.spec == spec:
                    # Same camera already running -- return it instead of binding the
                    # preview port a second time (that fails with Address already in use
                    # and is what made every Defaults keystroke look like a disconnect).
                    entry.last_seen = self._monotonic()
                    return preview_id, self._registry.labels(preview_id)
        meter = build(spec)
        handles = find_ocr_previews(meter)
        if not handles:
            self._close(meter)
            return None, ()
        preview_id = f"preview-{uuid.uuid4().hex}"
        self._registry.register(preview_id, handles)
        with self._lock:
            self._entries[preview_id] = _Entry(spec=spec, meter=meter, last_seen=self._monotonic())
        return preview_id, tuple(handle.label for handle in handles)

    def borrow(self, spec: PowerMeterSpec) -> PowerMeter | None:
        """Return a close-safe handle to a live preview that already owns this spec's port.

        Matching is by OCR preview bind address, not full spec equality: the setup
        preview is built from Defaults and the preflight request can differ in
        unrelated fields, but they still collide on ``127.0.0.1:8765``.
        """

        wanted = ocr_preview_binds(spec)
        if not wanted:
            return None
        with self._lock:
            for entry in self._entries.values():
                if wanted & ocr_preview_binds(entry.spec):
                    return _UncloseableMeter(entry.meter)
        return None

    def touch(self, preview_id: str) -> None:
        """Extend a preview's lifetime; called from every request that reads it."""
        with self._lock:
            entry = self._entries.get(preview_id)
            if entry is not None:
                entry.last_seen = self._monotonic()

    def labels(self, preview_id: str) -> tuple[str, ...]:
        return self._registry.labels(preview_id)

    def stop(self, preview_id: str) -> None:
        with self._lock:
            entry = self._entries.pop(preview_id, None)
        if entry is None:
            return
        self._registry.unregister(preview_id)
        self._close(entry.meter)

    def stop_all(self) -> None:
        """Close every live preview -- app shutdown only, not part of the request path."""
        with self._lock:
            entries = list(self._entries.items())
            self._entries.clear()
        for preview_id, entry in entries:
            self._registry.unregister(preview_id)
            self._close(entry.meter)

    def _reap_loop(self) -> None:
        while True:
            self._sleep(_REAP_INTERVAL_SECONDS)
            self._reap_once()

    def _reap_once(self) -> None:
        """One reaper sweep, split out from `_reap_loop` so a test can drive it directly
        against a fake clock instead of a real background thread and sleep interval."""
        now = self._monotonic()
        with self._lock:
            stale = [
                (preview_id, entry.meter)
                for preview_id, entry in self._entries.items()
                if now - entry.last_seen > IDLE_TIMEOUT_SECONDS
            ]
            for preview_id, _meter in stale:
                del self._entries[preview_id]
        for preview_id, meter in stale:
            self._registry.unregister(preview_id)
            self._close(meter)

    @staticmethod
    def _close(meter: PowerMeter) -> None:
        try:
            meter.close()
        except Exception as error:  # noqa: BLE001 - cleanup must not crash the reaper or the request
            _LOGGER.warning("Could not close a meter preview: %s", error)
