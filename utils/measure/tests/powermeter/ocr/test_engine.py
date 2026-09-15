from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from measure.powermeter.ocr.engine import RapidOcrEngine, TextBox
from measure.powermeter.ocr.layout import PR10
from measure.powermeter.ocr.reader import DisplayReader, rotate
import numpy as np
import pytest

from tests.powermeter.ocr.conftest import blank_frame

FIXTURES = Path(__file__).parents[2] / "fixtures" / "ocr"


@dataclass
class FakeRapidOcr:
    """Stands in for ``rapidocr.RapidOCR``: records the flags and returns scripted results."""

    results: list[object]
    calls: list[dict[str, bool]]

    def __call__(self, image: object, **flags: bool) -> object:
        self.calls.append(flags)
        return self.results.pop(0)


@pytest.fixture
def fake_rapidocr(monkeypatch: pytest.MonkeyPatch) -> FakeRapidOcr:
    fake = FakeRapidOcr(results=[], calls=[])
    module = ModuleType("rapidocr")
    module.RapidOCR = lambda: fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rapidocr", module)
    return fake


def test_detect_converts_boxes_and_passes_all_flags(fake_rapidocr: FakeRapidOcr) -> None:
    fake_rapidocr.results.append(
        SimpleNamespace(
            boxes=np.array([[[1, 2], [3, 2], [3, 4], [1, 4]]]),
            txts=("Power4.60w",),
            scores=[0.9],
        ),
    )
    engine = RapidOcrEngine()

    boxes = engine.detect(blank_frame())

    assert boxes == [TextBox(((1.0, 2.0), (3.0, 2.0), (3.0, 4.0), (1.0, 4.0)), "Power4.60w", 0.9)]
    assert fake_rapidocr.calls == [{"use_det": True, "use_cls": True, "use_rec": True}]


def test_detect_without_scores_or_boxes(fake_rapidocr: FakeRapidOcr) -> None:
    fake_rapidocr.results.extend(
        [
            SimpleNamespace(boxes=np.array([[[0, 0], [2, 0], [2, 1], [0, 1]]]), txts=("x",), scores=None),
            SimpleNamespace(boxes=None, txts=None),
            SimpleNamespace(txts=("no boxes attribute",)),
            SimpleNamespace(boxes=np.empty((0, 4, 2)), txts=()),
        ],
    )
    engine = RapidOcrEngine()
    assert engine.detect(blank_frame())[0].score == 1.0
    assert engine.detect(blank_frame()) == []
    assert engine.detect(blank_frame()) == []
    assert engine.detect(blank_frame()) == []


def test_recognize_joins_texts_and_skips_detection(fake_rapidocr: FakeRapidOcr) -> None:
    fake_rapidocr.results.extend([SimpleNamespace(txts=("V233.0v", "C0.055A")), SimpleNamespace(txts=None)])
    engine = RapidOcrEngine()

    assert engine.recognize(blank_frame()) == "V233.0vC0.055A"
    assert engine.recognize(blank_frame()) == ""
    assert fake_rapidocr.calls[0] == {"use_det": False, "use_cls": False, "use_rec": True}


@pytest.fixture(scope="module")
def real_engine() -> RapidOcrEngine:
    pytest.importorskip("rapidocr")
    return RapidOcrEngine()


@pytest.mark.parametrize(
    "name, expected",
    [
        ("pr10_4_60w.jpg", (4.60, 233.0, 0.055, 0.354)),
        ("pr10_4_95w.jpg", (4.95, 232.9, 0.034, 0.614)),
        ("pr10_1178w.jpg", (1178.0, 228.1, 5.191, 0.995)),
    ],
)
def test_real_engine_reads_the_spike_frames_in_every_orientation(
    real_engine: RapidOcrEngine,
    name: str,
    expected: tuple[float, float, float, float],
) -> None:
    """The display is located whichever way the camera is turned, and nothing wrong gets through.

    Rotated variants are produced from the three level fixtures so the repository holds
    only three images. A single frame may still be rejected (the recogniser drops a digit
    or a label now and then; the meter's windowed median is what absorbs that), so the
    test demands that every orientation locates, that most are accepted, and that every
    accepted reading is exactly right.
    """
    cv2 = pytest.importorskip("cv2")
    level = cv2.imread(str(FIXTURES / name))
    assert level is not None

    outcomes = []
    for turn in (0, 90, 180, 270):
        frame = rotate(level, -turn) if turn else level
        reader = DisplayReader(real_engine, PR10)
        assert reader.locate(frame) is not None, f"display not located in {name} turned {turn}"
        reading = reader.read(frame, timestamp=0.0)
        outcomes.append((turn, reading))
        if reading.accepted:
            assert (reading.power, reading.voltage, reading.current, reading.pf) == expected, f"{name} turned {turn}"

    rejected = [(turn, reading.reason) for turn, reading in outcomes if not reading.accepted]
    assert len(rejected) <= 1, f"{name}: {rejected}"
