from __future__ import annotations

import time
from typing import ClassVar

from measure.powermeter.errors import PowerMeterError
import measure.powermeter.ocr as ocr_package
from measure.powermeter.ocr import build_ocr_power_meter
from measure.powermeter.ocr.layout import PR10
from measure.powermeter.spec import OcrPowerMeterSpec
import pytest

from tests.powermeter.ocr.conftest import FakeEngine


class Recorder:
    """Stands in for a component with a ``start()``/``close()`` life cycle and records its construction."""

    instances: ClassVar[list[Recorder]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        Recorder.instances.append(self)

    def start(self) -> Recorder:
        self.started = True
        return self

    def close(self) -> None:
        self.closed = True

    @property
    def fps(self) -> float:
        return 0.0

    def wait_for_frame(self, after_sequence: int, timeout: float) -> None:
        time.sleep(min(timeout, 0.01))
        return


@pytest.fixture
def components(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[Recorder]]:
    Recorder.instances.clear()

    class FrameSourceStub(Recorder): ...

    class PreviewStub(Recorder): ...

    monkeypatch.setattr(ocr_package, "RapidOcrEngine", FakeEngine)
    monkeypatch.setattr(ocr_package, "FrameSource", FrameSourceStub)
    monkeypatch.setattr(ocr_package, "PreviewServer", PreviewStub)
    return {"frames": FrameSourceStub, "preview": PreviewStub}


def test_build_wires_spec_into_capture_reader_preview_and_meter(components: dict[str, type[Recorder]]) -> None:
    spec = OcrPowerMeterSpec(
        source="http://camera/",
        preview_host="192.168.0.5",
        preview_port=9000,
        window_seconds=2.0,
        stale_after_seconds=7.0,
        crosscheck_tolerance_pct=4.0,
        min_current_for_crosscheck=0.05,
    )

    meter = build_ocr_power_meter(spec)
    try:
        source = next(i for i in Recorder.instances if isinstance(i, components["frames"]))
        preview = next(i for i in Recorder.instances if isinstance(i, components["preview"]))
        assert source.args == ("http://camera/",)
        assert source.started
        assert preview.args == ("192.168.0.5", 9000)
        assert preview.started
        assert meter._window_seconds == 2.0  # noqa: SLF001
        assert meter._stale_after_seconds == 7.0  # noqa: SLF001
        assert meter._reader.layout is PR10  # noqa: SLF001
        assert meter._reader._crosscheck_tolerance_pct == 4.0  # noqa: SLF001
        assert meter._reader._min_current_for_crosscheck == 0.05  # noqa: SLF001
        assert meter._thread.is_alive()  # noqa: SLF001
    finally:
        meter.close()
    assert source.closed
    assert preview.closed


def test_build_without_preview(components: dict[str, type[Recorder]]) -> None:
    meter = build_ocr_power_meter(OcrPowerMeterSpec(preview_port=None))
    meter.close()
    assert not any(isinstance(i, components["preview"]) for i in Recorder.instances)


def test_build_rejects_an_unknown_layout(components: dict[str, type[Recorder]]) -> None:
    with pytest.raises(PowerMeterError, match="Unknown OCR display layout 'pr20'; known: pr10"):
        build_ocr_power_meter(OcrPowerMeterSpec(layout="pr20"))
    assert Recorder.instances == []
