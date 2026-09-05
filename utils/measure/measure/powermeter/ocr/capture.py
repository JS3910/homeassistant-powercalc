"""Continuously grab frames from a camera, stream or file and keep only the newest one."""

from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Protocol

import cv2
import numpy as np

from measure.powermeter.ocr.engine import Image

_LOGGER = logging.getLogger("measure")


@dataclass(frozen=True)
class Frame:
    image: Image
    timestamp: float
    sequence: int
    diff_mean: float
    """Mean absolute pixel difference from the previous frame; large when something moved."""


class FrameProvider(Protocol):
    """What the meter needs from a frame source; ``FrameSource`` is the real one."""

    def latest(self) -> Frame | None: ...

    def wait_for_frame(self, after_sequence: int, timeout: float) -> Frame | None: ...

    def close(self) -> None: ...

    @property
    def fps(self) -> float: ...


class VideoCapture(Protocol):
    """The part of ``cv2.VideoCapture`` the source uses, so tests can substitute a fake."""

    def isOpened(self) -> bool: ...  # noqa: N802

    def read(self) -> tuple[bool, Any]: ...

    def release(self) -> None: ...


def parse_source(source: str) -> int | str:
    """A source that is all digits is a local camera index; anything else is a URL or file path."""
    stripped = source.strip()
    return int(stripped) if stripped.isdigit() else stripped


def open_video_capture(source: int | str, timeout_seconds: float) -> VideoCapture:
    """Open a source so that a stream which stops answering fails instead of blocking forever.

    ``cv2.VideoCapture.read`` on a network stream otherwise blocks for as long as the
    server keeps the connection open without sending, and no thread can interrupt it.
    The FFMPEG backend accepts open and read timeouts; local cameras do not need them.
    """
    if isinstance(source, int):
        return cv2.VideoCapture(source)
    timeout_ms = int(timeout_seconds * 1000)
    return cv2.VideoCapture(
        source,
        cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms],
    )


class FrameSource:
    """Reads frames on a daemon thread so ``latest()`` is never behind the camera.

    ``cv2.VideoCapture`` buffers frames internally; reading them as fast as they arrive
    and keeping only the most recent one means a caller always sees what the display
    shows now, not what it showed a few frames ago. A source that stops delivering is
    reopened with backoff, and ``error`` says so, including while a read is still
    hanging, so the meter can report a stalled camera rather than an ageing reading.
    """

    def __init__(
        self,
        source: str,
        *,
        clock: Callable[[], float] = time.time,
        open_capture: Callable[[int | str], VideoCapture] | None = None,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 10.0,
        stall_seconds: float = 5.0,
    ) -> None:
        self._source = parse_source(source)
        self._clock = clock
        self._open_capture = open_capture or (lambda source: open_video_capture(source, stall_seconds))
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._stall_seconds = stall_seconds
        self._lock = threading.Condition()
        self._latest: Frame | None = None
        self._previous_grey: np.ndarray[Any, Any] | None = None
        self._sequence = 0
        self._frame_times: list[float] = []
        self._opened_at: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ocr-capture", daemon=True)
        self._error: str | None = None

    @property
    def source(self) -> int | str:
        return self._source

    @property
    def error(self) -> str | None:
        """Why no frames are coming, or ``None`` while the source delivers."""
        if self._error is not None:
            return self._error
        with self._lock:
            latest = self._latest
            opened_at = self._opened_at
        since = latest.timestamp if latest is not None else opened_at
        if since is None:
            return None
        silence = self._clock() - since
        if silence > self._stall_seconds:
            return f"no frame from video source {self._source!r} for {silence:.0f}s"
        return None

    def start(self) -> FrameSource:
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def wait_for_frame(self, after_sequence: int, timeout: float) -> Frame | None:
        """Block until a frame newer than ``after_sequence`` arrives, or return ``None`` on timeout."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._latest is None or self._latest.sequence <= after_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return None
                self._lock.wait(remaining)
            return self._latest

    @property
    def fps(self) -> float:
        with self._lock:
            times = list(self._frame_times)
        if len(times) < 2:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0]) if times[-1] > times[0] else 0.0

    def _run(self) -> None:
        delay = self._reconnect_delay
        while not self._stop.is_set():
            capture = self._open_capture(self._source)
            if not capture.isOpened():
                capture.release()
                self._error = f"cannot open video source {self._source!r}"
                _LOGGER.warning("%s; retrying in %.0fs", self._error, delay)
                self._stop.wait(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
                continue
            with self._lock:
                self._opened_at = self._clock()
            self._error = None
            delay = self._reconnect_delay
            _LOGGER.info("Video source %r opened", self._source)
            self._read_until_failure(capture)
            capture.release()
            if not self._stop.is_set():
                self._error = f"video source {self._source!r} stopped delivering frames"
                _LOGGER.warning("%s; reconnecting", self._error)
                self._stop.wait(delay)

    def _read_until_failure(self, capture: VideoCapture) -> None:
        """Read until the source fails repeatedly or has been silent for ``stall_seconds``."""
        failures = 0
        last_frame_at = self._clock()
        while not self._stop.is_set():
            ok, image = capture.read()
            if not ok or image is None:
                failures += 1
                if failures >= 5 or self._clock() - last_frame_at > self._stall_seconds:
                    return
                self._stop.wait(0.05)
                continue
            failures = 0
            last_frame_at = self._clock()
            self._publish(image)

    def _publish(self, image: Image) -> None:
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        small = cv2.resize(grey, (160, 120), interpolation=cv2.INTER_AREA).astype(np.int16)
        diff = float(np.abs(small - self._previous_grey).mean()) if self._previous_grey is not None else 0.0
        self._previous_grey = small
        now = self._clock()
        with self._lock:
            self._sequence += 1
            self._latest = Frame(image=image, timestamp=now, sequence=self._sequence, diff_mean=diff)
            self._frame_times.append(now)
            if len(self._frame_times) > 30:
                del self._frame_times[0]
            self._lock.notify_all()
