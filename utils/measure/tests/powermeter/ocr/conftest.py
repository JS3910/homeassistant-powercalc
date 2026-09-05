"""Shared fakes for the OCR meter tests: a scripted engine and synthetic frames."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from measure.powermeter.ocr.engine import Image, TextBox
from measure.powermeter.ocr.reader import Rect
import numpy as np
import pytest

pytest.importorskip("cv2")

# The PR10 rows as the fake engine "detects" them: one box per display row, stacked in
# a single column, roughly where they sit in the real 800x600 frames.
POWER_BOX = ((320.0, 194.0), (486.0, 207.0), (482.0, 251.0), (317.0, 238.0))
VI_BOX = ((362.0, 250.0), (539.0, 266.0), (536.0, 293.0), (360.0, 277.0))
PF_BOX = ((317.0, 280.0), (395.0, 287.0), (393.0, 313.0), (314.0, 306.0))
TITLE_BOX = ((343.0, 162.0), (539.0, 179.0), (537.0, 205.0), (341.0, 188.0))


def rows(power: str, vi: str, pf: str) -> list[TextBox]:
    return [
        TextBox(TITLE_BOX, "Real-Time Parameter"),
        TextBox(POWER_BOX, power),
        TextBox(VI_BOX, vi),
        TextBox(PF_BOX, pf),
    ]


def blank_frame(height: int = 600, width: int = 800, value: int = 40) -> Image:
    return np.full((height, width, 3), value, dtype=np.uint8)


def rect_of(points: Iterable[tuple[float, float]], margin: int = 6) -> Rect:
    xs = [int(p[0]) for p in points]
    ys = [int(p[1]) for p in points]
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


@dataclass
class FakeEngine:
    """Returns scripted detections and, for recognition, the text of whichever row the crop matches.

    ``recognitions`` maps a region (as produced by ``rect_of``) to the text to return for
    it; a crop of a region not in the map returns ``default``. ``detections`` is popped
    from the front on each ``detect`` call, the last entry repeating.
    """

    detections: list[list[TextBox]] = field(default_factory=list)
    recognitions: dict[Rect, str] = field(default_factory=dict)
    default: str = ""
    detect_calls: int = 0
    recognize_calls: int = 0
    last_crop_shapes: list[tuple[int, ...]] = field(default_factory=list)

    def detect(self, image: Image) -> list[TextBox]:
        self.detect_calls += 1
        if not self.detections:
            return []
        if len(self.detections) > 1:
            return self.detections.pop(0)
        return self.detections[0]

    def recognize(self, image: Image) -> str:
        self.recognize_calls += 1
        self.last_crop_shapes.append(image.shape)
        # The reader upscales crops by 2 and pads by the margin; the crop's size identifies the region.
        for rect, text in self.recognitions.items():
            x1, y1, x2, y2 = rect
            if image.shape[0] == (y2 - y1) * 2 and image.shape[1] == (x2 - x1) * 2:
                return text
        return self.default


def scripted_engine(power: str, vi: str, pf: str, *, title: str = "Real-Time Parameter") -> FakeEngine:
    """An engine that detects the standard rows and recognises the same texts per region."""
    boxes = rows(power, vi, pf)
    boxes[0] = TextBox(TITLE_BOX, title)
    return FakeEngine(
        detections=[boxes],
        recognitions={rect_of(POWER_BOX): power, rect_of(VI_BOX): vi, rect_of(PF_BOX): pf},
    )
