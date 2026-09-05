from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
import threading
import time

import cv2
from measure.powermeter.ocr.capture import FrameSource, open_video_capture, parse_source
from measure.powermeter.ocr.engine import Image
import numpy as np
import pytest

from tests.powermeter.ocr.conftest import blank_frame


@pytest.mark.parametrize(
    "source, expected",
    [
        ("0", 0),
        (" 2 ", 2),
        ("http://camera.promethen.com:8080/", "http://camera.promethen.com:8080/"),
        ("clip.mp4", "clip.mp4"),
        ("", ""),
    ],
)
def test_parse_source(source: str, expected: int | str) -> None:
    assert parse_source(source) == expected


class Clock:
    """A clock the test controls; the fake capture advances it half a second per frame it yields."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeCapture:
    """Yields scripted frames; ``None`` entries are read failures, exhaustion is a dead stream."""

    frames: Iterator[Image | None]
    opened: bool = True
    released: bool = False
    reads: int = 0
    clock: Clock | None = None

    def isOpened(self) -> bool:  # noqa: N802
        return self.opened

    def read(self) -> tuple[bool, Image | None]:
        self.reads += 1
        image = next(self.frames, None)
        if image is not None and self.clock is not None:
            self.clock.now += 0.5
        return (image is not None, image)

    def release(self) -> None:
        self.released = True


@dataclass
class Opener:
    captures: list[FakeCapture]
    opened_with: list[int | str] = field(default_factory=list)
    exhausted = threading.Event()

    def __call__(self, source: int | str) -> FakeCapture:
        self.opened_with.append(source)
        if self.captures:
            return self.captures.pop(0)
        self.exhausted.set()
        return FakeCapture(iter(()), opened=False)


def wait_until(predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


def test_frames_are_published_with_sequence_timestamp_and_change_measure() -> None:
    dark = blank_frame(value=40)
    bright = blank_frame(value=140)
    clock = Clock()
    capture = FakeCapture(iter([dark, dark, bright]), clock=clock)
    opener = Opener([capture])
    source = FrameSource("0", clock=clock, open_capture=opener, reconnect_delay=0.01, max_reconnect_delay=0.02)

    assert source.latest() is None
    assert source.fps == 0.0
    source.start()
    try:
        third = source.wait_for_frame(2, timeout=5.0)
        assert third is not None
        assert third.sequence == 3
        assert third.timestamp == 1001.5
        assert third.diff_mean == pytest.approx(100.0)
        assert source.latest() is third
        assert source.fps == pytest.approx(2.0)  # 3 frames, 0.5 s apart on the fake clock
        assert opener.opened_with[0] == 0
        wait_until(lambda: capture.released)
    finally:
        source.close()


def test_first_frame_has_zero_diff_and_greyscale_input_is_accepted() -> None:
    grey = np.full((600, 800), 40, dtype=np.uint8)
    opener = Opener([FakeCapture(iter([grey]))])
    source = FrameSource("0", clock=Clock(), open_capture=opener, reconnect_delay=0.01).start()
    try:
        first = source.wait_for_frame(0, timeout=5.0)
        assert first is not None
        assert first.diff_mean == 0.0
        assert first.image is grey
    finally:
        source.close()


def test_fps_is_measured_over_the_last_thirty_frames() -> None:
    clock = Clock()
    opener = Opener([FakeCapture(iter([blank_frame()] * 40), clock=clock)])
    source = FrameSource("0", clock=clock, open_capture=opener, reconnect_delay=0.01).start()
    try:
        last = source.wait_for_frame(39, timeout=5.0)
        assert last is not None
        assert last.sequence == 40
        assert source.fps == pytest.approx(2.0)
        assert len(source._frame_times) == 30  # noqa: SLF001
    finally:
        source.close()


def test_wait_for_frame_times_out_when_nothing_newer_arrives() -> None:
    opener = Opener([FakeCapture(iter([blank_frame()]))])
    source = FrameSource("0", clock=Clock(), open_capture=opener, reconnect_delay=0.01).start()
    try:
        first = source.wait_for_frame(0, timeout=5.0)
        assert first is not None
        assert source.wait_for_frame(first.sequence, timeout=0.05) is None
    finally:
        source.close()


def test_reconnects_when_the_source_cannot_be_opened_or_stops_delivering(caplog: pytest.LogCaptureFixture) -> None:
    unopenable = FakeCapture(iter(()), opened=False)
    dead_after_one = FakeCapture(iter([blank_frame()]))
    healthy = FakeCapture(iter([blank_frame(), blank_frame()]))
    opener = Opener([unopenable, dead_after_one, healthy])
    source = FrameSource(
        "rtsp://camera/stream",
        clock=Clock(),
        open_capture=opener,
        reconnect_delay=0.01,
        max_reconnect_delay=0.02,
    )
    source.start()
    try:
        wait_until(opener.exhausted.is_set)
        assert opener.opened_with[:3] == ["rtsp://camera/stream"] * 3
        assert unopenable.released
        assert dead_after_one.released
        assert dead_after_one.reads >= 6  # one frame, then five failed reads before giving up
        assert "cannot open video source 'rtsp://camera/stream'; retrying" in caplog.text
        assert "stopped delivering frames; reconnecting" in caplog.text
        wait_until(lambda: source.error == "cannot open video source 'rtsp://camera/stream'")
        latest = source.latest()
        assert latest is not None
        assert latest.sequence == 3
    finally:
        source.close()


def test_error_reports_a_source_that_went_silent() -> None:
    """A stream that keeps the connection open without sending is a stall, not a failure the read reports."""

    class HangingCapture(FakeCapture):
        def read(self) -> tuple[bool, Image | None]:
            ok, image = super().read()
            if not ok:
                time.sleep(0.05)  # stands in for a blocking read
            return ok, image

    clock = Clock()
    opener = Opener([HangingCapture(iter([blank_frame()]))])
    source = FrameSource("rtsp://camera/stream", clock=clock, open_capture=opener, reconnect_delay=5, stall_seconds=3.0)
    assert source.error is None  # not started: nothing expected yet
    source.start()
    try:
        first = source.wait_for_frame(0, timeout=5.0)
        assert first is not None
        assert source.error is None
        clock.now += 10
        error = FrameSource.error.fget(source)  # type: ignore[attr-defined]  # re-read; mypy assumes the property is stable
        assert error is not None
        assert error.startswith("no frame from video source 'rtsp://camera/stream' for 10s")
        # The read loop notices the same silence and gives the source up for a reconnect.
        wait_until(lambda: "stopped delivering frames" in (source.error or ""))
    finally:
        source.close()


def test_error_counts_silence_from_opening_when_no_frame_ever_arrived() -> None:
    class SilentCapture(FakeCapture):
        def read(self) -> tuple[bool, Image | None]:
            time.sleep(0.02)
            return False, None

    clock = Clock()
    opener = Opener([SilentCapture(iter(()))])
    source = FrameSource("0", clock=clock, open_capture=opener, reconnect_delay=5, stall_seconds=3.0).start()
    try:
        wait_until(lambda: source.error is not None)
        assert source.error is not None
        assert source.error.startswith("no frame from video source 0 for ") or "stopped delivering" in source.error
    finally:
        source.close()


def test_open_video_capture_uses_ffmpeg_timeouts_for_streams_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    class RecordingCapture:
        def __init__(self, *args: object) -> None:
            calls.append(args)

    monkeypatch.setattr(cv2, "VideoCapture", RecordingCapture)

    open_video_capture("http://camera/", 5.0)
    open_video_capture(1, 5.0)

    assert calls == [
        (
            "http://camera/",
            cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
        ),
        (1,),
    ]


def test_default_opener_passes_the_stall_time_as_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    class RecordingCapture:
        def __init__(self, *args: object) -> None:
            calls.append(args)

        def isOpened(self) -> bool:  # noqa: N802
            return False

        def release(self) -> None:
            pass

    monkeypatch.setattr(cv2, "VideoCapture", RecordingCapture)
    source = FrameSource("http://camera/", stall_seconds=2.5, reconnect_delay=0.01).start()
    try:
        wait_until(lambda: bool(calls))
    finally:
        source.close()
    assert calls[0][2] == [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 2500, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2500]


def test_close_is_idempotent_and_stops_the_thread() -> None:
    opener = Opener([FakeCapture(iter([blank_frame()]))])
    source = FrameSource("0", clock=Clock(), open_capture=opener, reconnect_delay=0.01).start()
    source.close()
    source.close()
    assert not any(thread.name == "ocr-capture" for thread in threading.enumerate())
    assert source.source == 0
