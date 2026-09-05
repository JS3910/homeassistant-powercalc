"""Locate a meter display in a camera frame and read its fields.

Locating runs full text detection, levels the frame by the detected text's angle, and
matches the recognised rows to the layout's fields; it is slow (about 150 ms per
attempt) and only needed when the meter or camera moved. Reading runs recognition on the
located regions only, about 10 ms per frame.
"""

from dataclasses import dataclass, field
from itertools import pairwise
import logging
import statistics
from typing import cast

import cv2

from measure.powermeter.ocr.engine import Image, OcrEngine, TextBox
from measure.powermeter.ocr.layout import DisplayLayout

_LOGGER = logging.getLogger("measure")

Rect = tuple[int, int, int, int]
"""x1, y1, x2, y2 in pixels of the levelled frame."""

# Below this the frame is used as is; rotating by a fraction of a degree costs time and
# blurs the digits without helping recognition.
_LEVEL_THRESHOLD_DEGREES = 1.0
# Up to this tilt the axis-aligned crops still contain whole rows and the recogniser
# reads them; beyond it the frame is levelled before locating.
_TILT_TOLERANCE_DEGREES = 8.0


@dataclass(frozen=True)
class Location:
    """Where the layout's fields are in the levelled frame, and how much levelling that took."""

    angle: float
    regions: dict[str, Rect]
    boxes: tuple[TextBox, ...] = ()

    def rects(self) -> list[Rect]:
        """Distinct regions to recognise; several fields may share one row."""
        seen: list[Rect] = []
        for rect in self.regions.values():
            if rect not in seen:
                seen.append(rect)
        return seen


@dataclass(frozen=True)
class DisplayReading:
    """Everything read from one frame, accepted or not."""

    timestamp: float
    accepted: bool
    reason: str | None = None
    power: float | None = None
    voltage: float | None = None
    current: float | None = None
    pf: float | None = None
    raw: dict[str, str] = field(default_factory=dict)
    """Recognised text per field."""
    location: Location | None = None


def rotate(image: Image, angle: float) -> Image:
    """Rotate about the centre, enlarging the canvas so nothing is cut off."""
    if abs(angle) < _LEVEL_THRESHOLD_DEGREES:
        return image
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_width = int(height * sin + width * cos)
    new_height = int(height * cos + width * sin)
    matrix[0, 2] += new_width / 2 - width / 2
    matrix[1, 2] += new_height / 2 - height / 2
    return cast(Image, cv2.warpAffine(image, matrix, (new_width, new_height), borderValue=(40, 40, 40)))


def _normalise_angle(angle: float) -> float:
    return (angle + 180) % 360 - 180


class DisplayReader:
    def __init__(
        self,
        engine: OcrEngine,
        layout: DisplayLayout,
        *,
        crosscheck_tolerance_pct: float = 3.0,
        min_current_for_crosscheck: float = 0.02,
        margin: int = 6,
        upscale: float = 2.0,
    ) -> None:
        self._engine = engine
        self._layout = layout
        self._crosscheck_tolerance_pct = crosscheck_tolerance_pct
        self._min_current_for_crosscheck = min_current_for_crosscheck
        self._margin = margin
        self._upscale = upscale
        self.location: Location | None = None

    @property
    def layout(self) -> DisplayLayout:
        return self._layout

    def invalidate(self) -> None:
        """Forget the located regions so the next frame is located afresh."""
        self.location = None

    def locate(self, frame: Image) -> Location | None:
        """Find every field of the layout in the frame, levelling it first if the text is tilted.

        A text line's angle is ambiguous modulo 180 degrees, and near-vertical text may be
        either way up, so the candidate orientations are tried in turn until one yields
        the layout's rows in the right order.
        """
        boxes = self._engine.detect(frame)
        if not boxes:
            self.location = None
            return None
        angles = [box.angle for box in boxes if len(box.text.strip()) >= 3]
        tilt = statistics.median(angles) if angles else 0.0
        # The recogniser copes with a small tilt by itself and rotating blurs the digits,
        # so a nearly level frame is used as it is; beyond that the frame is levelled
        # first, because the regions read per frame are axis-aligned crops.
        candidates = [0.0] if abs(tilt) < _TILT_TOLERANCE_DEGREES else []
        for candidate in (tilt, tilt + 180, tilt + 90, tilt - 90, 0.0):
            angle = _normalise_angle(candidate)
            if all(abs(_normalise_angle(angle - tried)) >= _LEVEL_THRESHOLD_DEGREES for tried in candidates):
                candidates.append(angle)
        best: Location | None = None
        for angle in candidates:
            levelled_boxes = boxes if angle == 0.0 else self._engine.detect(rotate(frame, angle))
            location = self._match(angle, levelled_boxes)
            if location is None:
                continue
            if best is None or len(location.regions) > len(best.regions):
                best = location
            if len(location.regions) == len(self._layout.fields):
                break
        if best is None or len(best.regions) != len(self._layout.fields):
            _LOGGER.debug("Display not located: %s", [box.text for box in boxes])
            self.location = None
            return None
        _LOGGER.info(
            "Display located: levelled by %.1f°, %s",
            best.angle,
            ", ".join(f"{name} at {rect}" for name, rect in best.regions.items()),
        )
        self.location = best
        return best

    def _match(self, angle: float, boxes: list[TextBox]) -> Location | None:
        regions = self._regions_by_label(boxes)
        # A field whose own label was not read still sits on the row of its siblings.
        for group in self._layout.row_order:
            found = [regions[name] for name in group if name in regions]
            if found:
                for name in group:
                    regions.setdefault(name, found[0])
        if not regions:
            return None
        if len(regions) == len(self._layout.fields) and not self._rows_in_order(regions):
            return None
        return Location(angle=angle, regions=regions, boxes=tuple(boxes))

    def _regions_by_label(self, boxes: list[TextBox]) -> dict[str, Rect]:
        """The region of each field whose label was read; a row whose value parses beats a label-only one ("Veg")."""
        regions: dict[str, Rect] = {}
        for require_value in (True, False):
            for box in boxes:
                for name in self._layout.fields_in(box.text):
                    if name in regions or (require_value and self._layout.parse(name, box.text) is None):
                        continue
                    regions[name] = self._padded(box.bounds)
        return regions

    def _rows_in_order(self, regions: dict[str, Rect]) -> bool:
        """Rows must appear top to bottom in layout order and line up in one column."""
        centres = []
        for group in self._layout.row_order:
            rect = regions[group[0]]
            centres.append((rect[1] + rect[3]) / 2)
        if any(later <= earlier for earlier, later in pairwise(centres)):
            return False
        x1 = max(rect[0] for rect in regions.values())
        x2 = min(rect[2] for rect in regions.values())
        return x1 < x2

    def _padded(self, bounds: tuple[int, int, int, int]) -> Rect:
        x1, y1, x2, y2 = bounds
        return (max(0, x1 - self._margin), max(0, y1 - self._margin), x2 + self._margin, y2 + self._margin)

    def read(self, frame: Image, timestamp: float) -> DisplayReading:
        """Recognise the located regions in a frame and validate the values against each other."""
        location = self.location
        if location is None:
            return DisplayReading(timestamp=timestamp, accepted=False, reason="display not located")
        levelled = rotate(frame, location.angle)
        texts: dict[Rect, str] = {}
        for rect in location.rects():
            texts[rect] = self._recognise(levelled, rect)
        raw = {name: texts[rect] for name, rect in location.regions.items()}

        values: dict[str, float] = {}
        for name in self._layout.fields:
            parsed = self._layout.parse(name, raw[name])
            if parsed is None:
                return DisplayReading(
                    timestamp=timestamp,
                    accepted=False,
                    reason=f"{name} unreadable: {raw[name]!r}",
                    raw=raw,
                    location=location,
                )
            values[name] = float(parsed)

        reason = self._crosscheck(values)
        return DisplayReading(
            timestamp=timestamp,
            accepted=reason is None,
            reason=reason,
            power=values.get("power"),
            voltage=values.get("voltage"),
            current=values.get("current"),
            pf=values.get("pf"),
            raw=raw,
            location=location,
        )

    def _recognise(self, levelled: Image, rect: Rect) -> str:
        height, width = levelled.shape[:2]
        x1, y1, x2, y2 = rect
        crop = levelled[max(0, y1) : min(height, y2), max(0, x1) : min(width, x2)]
        if crop.size == 0:
            return ""
        if self._upscale != 1.0:
            crop = cast(
                Image, cv2.resize(crop, None, fx=self._upscale, fy=self._upscale, interpolation=cv2.INTER_CUBIC)
            )
        return self._engine.recognize(crop).replace(" ", "")

    def _crosscheck(self, values: dict[str, float]) -> str | None:
        """Power must equal voltage x current x power factor; a misread digit breaks that."""
        if not {"power", "voltage", "current", "pf"} <= values.keys():
            return None
        power, voltage, current, pf = values["power"], values["voltage"], values["current"], values["pf"]
        if current < self._min_current_for_crosscheck:
            return None
        expected = voltage * current * pf
        scale = max(expected, power)
        if scale <= 0:
            return None
        deviation_pct = abs(power - expected) / scale * 100
        if deviation_pct > self._crosscheck_tolerance_pct:
            return (
                f"power {power:g} W disagrees with V x I x PF = {voltage:g} x {current:g} x {pf:g} = {expected:.2f} W "
                f"({deviation_pct:.1f}% > {self._crosscheck_tolerance_pct:g}%)"
            )
        return None
