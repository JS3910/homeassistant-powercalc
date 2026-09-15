"""Reach the OCR meter(s) in play for a running session, in-process.

`OcrPowerMeter` already publishes annotated frames and state into a `PreviewServer`
(``measure/powermeter/ocr/preview.py``). This module finds those `PreviewServer`
instances inside whatever meter tree a session assembled — a bare OCR meter, or one
wrapped as the primary or a witness of a `CompositePowerMeter` — and makes them
reachable by session id for the app's own API routes, without a second HTTP socket.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import TYPE_CHECKING

from measure.powermeter.composite import CompositePowerMeter
from measure.powermeter.powermeter import PowerMeter

if TYPE_CHECKING:
    # Real imports would require the optional `ocr` extra (it pulls in cv2) just to
    # start the app at all, even for a session with no OCR meter configured. This
    # module must stay importable without it -- see the try/except below.
    from measure.powermeter.ocr.preview import PreviewServer

try:
    from measure.powermeter.ocr.meter import OcrPowerMeter
except ImportError:
    OcrPowerMeter = None  # type: ignore[assignment, misc]


@dataclass(frozen=True)
class OcrPreviewHandle:
    """One OCR meter's preview, labelled by its role in the meter tree."""

    label: str
    preview: PreviewServer


def find_ocr_previews(meter: PowerMeter) -> tuple[OcrPreviewHandle, ...]:
    """Return a handle for every OCR meter reachable from ``meter``, labelled by role.

    Returns nothing, rather than raising, when the `ocr` extra isn't installed --
    a session simply has no OCR previews to offer in that case, same as one that
    never configured an OCR meter at all.
    """

    if isinstance(meter, CompositePowerMeter):
        handles = [OcrPreviewHandle(label="primary", preview=h.preview) for h in find_ocr_previews(meter.primary)]
        for witness in meter.witnesses:
            handles.extend(
                OcrPreviewHandle(label=witness.name, preview=h.preview) for h in find_ocr_previews(witness.meter)
            )
        return tuple(handles)
    if OcrPowerMeter is not None and isinstance(meter, OcrPowerMeter) and meter.preview is not None:
        return (OcrPreviewHandle(label="primary", preview=meter.preview),)
    return ()


class OcrPreviewRegistry:
    """Maps a running session id to whatever OCR preview(s) it assembled, if any."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, tuple[OcrPreviewHandle, ...]] = {}

    def register(self, session_id: str, handles: tuple[OcrPreviewHandle, ...]) -> None:
        if not handles:
            return
        with self._lock:
            self._by_session[session_id] = handles

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._by_session.pop(session_id, None)

    def get(self, session_id: str, label: str) -> PreviewServer | None:
        with self._lock:
            handles = self._by_session.get(session_id, ())
        for handle in handles:
            if handle.label == label:
                return handle.preview
        return None

    def labels(self, session_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(handle.label for handle in self._by_session.get(session_id, ()))
