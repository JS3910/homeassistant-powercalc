"""Camera-based power meter: reads the display of a physical meter with OCR.

Importing this package requires the ``ocr`` extra (``rapidocr``, ``onnxruntime``,
``numpy`` and the ``opencv-python`` they bring). The assembler imports it only when an
OCR meter is configured.
"""

from measure.powermeter.errors import PowerMeterError
from measure.powermeter.ocr.capture import FrameSource
from measure.powermeter.ocr.engine import RapidOcrEngine
from measure.powermeter.ocr.layout import LAYOUTS
from measure.powermeter.ocr.meter import OcrPowerMeter
from measure.powermeter.ocr.preview import PreviewServer
from measure.powermeter.ocr.reader import DisplayReader
from measure.powermeter.spec import OcrPowerMeterSpec

__all__ = ["OcrPowerMeter", "build_ocr_power_meter"]


def build_ocr_power_meter(spec: OcrPowerMeterSpec) -> OcrPowerMeter:
    """Wire capture, reader, preview and meter from a spec and start them."""
    try:
        layout = LAYOUTS[spec.layout]
    except KeyError as error:
        raise PowerMeterError(
            f"Unknown OCR display layout {spec.layout!r}; known: {', '.join(sorted(LAYOUTS))}"
        ) from error
    engine = RapidOcrEngine()
    reader = DisplayReader(
        engine,
        layout,
        crosscheck_tolerance_pct=spec.crosscheck_tolerance_pct,
        min_current_for_crosscheck=spec.min_current_for_crosscheck,
    )
    preview = PreviewServer(spec.preview_host, spec.preview_port).start() if spec.preview_port is not None else None
    frames = FrameSource(spec.source).start()
    return OcrPowerMeter(
        frames,
        reader,
        window_seconds=spec.window_seconds,
        stale_after_seconds=spec.stale_after_seconds,
        preview=preview,
    ).start()
