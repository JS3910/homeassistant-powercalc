from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

from measure.powermeter.errors import OutdatedMeasurementError, PowerMeterError
from measure.powermeter.ocr.capture import Frame
from measure.powermeter.ocr.layout import PR10
from measure.powermeter.ocr.meter import OcrPowerMeter
from measure.powermeter.ocr.reader import DisplayReader
import pytest

from tests.powermeter.ocr.conftest import (
    PF_BOX,
    POWER_BOX,
    VI_BOX,
    FakeEngine,
    blank_frame,
    rect_of,
    rows,
    scripted_engine,
)


@dataclass
class FakeFrames:
    """A frame provider fed by the test; ``wait_for_frame`` hands out queued frames in order."""

    queue: list[Frame] = field(default_factory=list)
    fps: float = 2.2
    error: str | None = None
    closed: bool = False

    def latest(self) -> Frame | None:
        return self.queue[-1] if self.queue else None

    def wait_for_frame(self, after_sequence: int, timeout: float) -> Frame | None:
        for frame in self.queue:
            if frame.sequence > after_sequence:
                return frame
        time.sleep(min(timeout, 0.01))
        return None

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def frame(sequence: int, timestamp: float, diff_mean: float = 0.0) -> Frame:
    return Frame(image=blank_frame(), timestamp=timestamp, sequence=sequence, diff_mean=diff_mean)


def make_meter(engine: FakeEngine, clock: Clock, **kwargs: float) -> tuple[OcrPowerMeter, FakeFrames]:
    frames = FakeFrames()
    meter = OcrPowerMeter(frames, DisplayReader(engine, PR10), clock=clock, **kwargs)  # type: ignore[arg-type]
    return meter, frames


def test_get_power_is_the_median_of_the_window_and_voltage_follows() -> None:
    clock = Clock()
    engine = scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354")
    meter, _ = make_meter(engine, clock, window_seconds=1.5)

    meter.process(frame(1, 998.0))  # outside the window
    engine.recognitions[rect_of(POWER_BOX)] = "Power4.62w"
    meter.process(frame(2, 999.0))
    engine.recognitions[rect_of(POWER_BOX)] = "Power4.70w"
    engine.recognitions[rect_of(VI_BOX)] = "V232.6vC0.056A"
    meter.process(frame(3, 999.5))
    engine.recognitions[rect_of(POWER_BOX)] = "Power4.61w"
    meter.process(frame(4, 1000.0))

    result = meter.get_power(include_voltage=True)

    assert result.power == 4.62
    assert result.voltage == 232.6
    assert result.updated == 1000.0
    assert meter.get_power().voltage is None
    assert meter.has_voltage_support()

    # Current and power factor are already computed for every accepted reading's
    # crosscheck, so they're reported regardless of include_voltage -- unlike voltage,
    # which is deliberately gated because most callers don't want the extra median cost.
    no_voltage_result = meter.get_power()
    assert no_voltage_result.current == 0.056
    assert no_voltage_result.power_factor == 0.354


def test_get_power_falls_back_to_the_newest_accepted_reading_within_stale_after() -> None:
    clock = Clock()
    meter, _ = make_meter(
        scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354"), clock, window_seconds=1.5, stale_after_seconds=5.0
    )
    meter.process(frame(1, 997.0))

    assert meter.get_power().power == 4.6

    clock.now = 1002.5
    with pytest.raises(OutdatedMeasurementError, match=r"last accepted reading is 5.5s old; last frame: ok"):
        meter.get_power()


def test_get_power_before_any_frame_or_acceptance_explains_why() -> None:
    clock = Clock()
    engine = FakeEngine(detections=[rows("Power4.60w", "V233.0vC0.055A", "PF0.354")], default="")
    meter, frames = make_meter(engine, clock)

    with pytest.raises(OutdatedMeasurementError, match="no frame processed yet"):
        meter.get_power()
    with pytest.raises(PowerMeterError, match="no frame processed yet"):
        meter.diagnostic_sample()

    meter.process(frame(1, 1000.0))
    with pytest.raises(OutdatedMeasurementError, match="no accepted reading yet; last frame: power unreadable: ''"):
        meter.get_power()

    frames.error = "cannot open video source 'x'"
    with pytest.raises(OutdatedMeasurementError, match="OCR: cannot open video source 'x'"):
        meter.get_power()


def test_relocates_after_repeated_rejections_and_on_a_frame_change() -> None:
    clock = Clock()
    engine = scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354")
    meter, _ = make_meter(engine, clock, relocate_after_rejections=3, relocate_diff_threshold=8.0)

    assert meter.process(frame(1, 1000.0)).accepted
    assert engine.detect_calls == 1  # initial location

    engine.recognitions[rect_of(POWER_BOX)] = "Pom"
    for sequence in (2, 3, 4):
        assert not meter.process(frame(sequence, 1000.0 + sequence)).accepted
    assert engine.detect_calls == 1
    meter.process(frame(5, 1005.0))
    assert engine.detect_calls == 2  # three rejections in a row triggered a re-location

    engine.recognitions[rect_of(POWER_BOX)] = "Power4.60w"
    assert meter.process(frame(6, 1006.0, diff_mean=12.0)).accepted
    assert engine.detect_calls == 3  # the picture changed a lot
    assert meter.state()["relocations"] == 3


def test_state_and_diagnostic_sample_reflect_the_latest_frame() -> None:
    clock = Clock()
    engine = scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354")
    meter, _ = make_meter(engine, clock)
    meter.process(frame(1, 999.0))
    engine.recognitions[rect_of(PF_BOX)] = "PF"
    meter.process(frame(2, 999.5))
    clock.now = 1000.0

    state = meter.state()
    assert state["frames"] == 2
    assert state["accepted"] == 1
    assert state["rejected"] == 1
    assert state["power"] == 4.6
    assert state["frame_age"] == 0.5
    assert state["accepted_age"] == 1.0
    assert state["fps"] == 2.2
    assert state["angle"] == 0.0
    assert state["source_error"] is None
    assert state["last"]["accepted"] is False
    assert state["last"]["reason"] == "pf unreadable: 'PF'"
    assert state["last"]["raw"] == {
        "power": "Power4.60w",
        "voltage": "V233.0vC0.055A",
        "current": "V233.0vC0.055A",
        "pf": "PF",
    }

    sample = meter.diagnostic_sample()
    assert sample.power == 4.6
    assert sample.raw_value == "Power4.60w"
    assert sample.reported_at == 999.0


def test_state_before_any_frame() -> None:
    meter, _ = make_meter(FakeEngine(), Clock())
    state = meter.state()
    assert state["last"] is None
    assert state["power"] is None
    assert state["frame_age"] is None
    assert state["angle"] is None


def test_background_thread_processes_frames_and_close_stops_everything() -> None:
    clock = Clock()
    engine = scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354")
    meter, frames = make_meter(engine, clock)
    frames.queue.append(frame(1, 1000.0))

    meter.start()
    deadline = time.monotonic() + 5
    while meter.state()["frames"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    meter.close()

    assert meter.state()["accepted"] == 1
    assert frames.closed
    assert not any(thread.name == "ocr-reader" for thread in threading.enumerate())


def test_background_thread_survives_a_processing_error(caplog: pytest.LogCaptureFixture) -> None:
    class ExplodingFrames(FakeFrames):
        def wait_for_frame(self, after_sequence: int, timeout: float) -> Frame | None:
            if after_sequence == 0:
                return Frame(image=None, timestamp=1000.0, sequence=1, diff_mean=0.0)  # type: ignore[arg-type]
            return super().wait_for_frame(after_sequence, timeout)

    frames = ExplodingFrames()
    meter = OcrPowerMeter(
        frames, DisplayReader(scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354"), PR10), clock=Clock()
    )
    meter.start()
    deadline = time.monotonic() + 5
    while "OCR frame processing failed" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.01)
    meter.close()

    assert "OCR frame processing failed" in caplog.text


def test_preview_receives_every_processed_frame() -> None:
    published: list[dict[str, object]] = []

    class FakePreview:
        def publish(self, image: object, state: dict[str, object]) -> None:
            published.append(state)

        def close(self) -> None:
            published.append({"closed": True})

    engine = scripted_engine("Power4.60w", "V233.0vC0.055A", "PF0.354")
    frames = FakeFrames()
    meter = OcrPowerMeter(frames, DisplayReader(engine, PR10), preview=FakePreview(), clock=Clock())  # type: ignore[arg-type]
    meter.process(frame(1, 1000.0))
    meter.close()

    assert published[0]["accepted"] == 1
    assert published[-1] == {"closed": True}
