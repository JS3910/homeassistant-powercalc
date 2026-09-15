"""OCR engine boundary: text detection on a whole frame, text recognition on a crop."""

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

Image = NDArray[np.uint8]


@dataclass(frozen=True)
class TextBox:
    """One detected line of text: its quadrilateral (4 points, x/y) and what it says."""

    points: tuple[tuple[float, float], ...]
    text: str
    score: float = 1.0

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))

    @property
    def angle(self) -> float:
        """Baseline angle in degrees, in (-90, 90], from the longer edge of the quadrilateral."""
        p = np.asarray(self.points, dtype=float)
        e1, e2 = p[1] - p[0], p[2] - p[1]
        edge = e1 if np.linalg.norm(e1) >= np.linalg.norm(e2) else e2
        angle = float(np.degrees(np.arctan2(edge[1], edge[0])))
        return (angle + 90) % 180 - 90


class OcrEngine(Protocol):
    def detect(self, image: Image) -> list[TextBox]:
        """Find and read every line of text in a frame."""

    def recognize(self, image: Image) -> str:
        """Read the text in a crop known to contain exactly one line."""


class RapidOcrEngine:
    """RapidOCR (PaddleOCR models on ONNX Runtime): pure Python wheels, no system dependency."""

    def __init__(self) -> None:
        from rapidocr import RapidOCR

        self._ocr: Any = RapidOCR()

    def detect(self, image: Image) -> list[TextBox]:
        # rapidocr stores the use_* flags of each call on the instance, so a
        # recognition-only call would switch detection off for every later call unless
        # the flags are passed explicitly every time.
        result = self._ocr(image, use_det=True, use_cls=True, use_rec=True)
        # Without detections rapidocr returns a result type that has no boxes at all.
        boxes = getattr(result, "boxes", None)
        txts = getattr(result, "txts", None)
        if boxes is None or txts is None or len(boxes) == 0:
            return []
        scores = getattr(result, "scores", None) or [1.0] * len(txts)
        return [
            TextBox(points=tuple((float(x), float(y)) for x, y in box), text=str(text), score=float(score))
            for box, text, score in zip(boxes, txts, scores, strict=True)
        ]

    def recognize(self, image: Image) -> str:
        result = self._ocr(image, use_det=False, use_cls=False, use_rec=True)
        return "".join(getattr(result, "txts", None) or ())
