from __future__ import annotations

from dataclasses import dataclass, field

from measure.ha_app.meter_preview import IDLE_TIMEOUT_SECONDS, MeterPreviewService
from measure.ha_app.ocr_preview import OcrPreviewRegistry
from measure.powermeter.dummy import DummyPowerMeter
from measure.powermeter.ocr.capture import Frame
from measure.powermeter.ocr.layout import PR10
from measure.powermeter.ocr.meter import OcrPowerMeter
from measure.powermeter.ocr.reader import DisplayReader
from measure.powermeter.spec import DummyPowerMeterSpec, OcrPowerMeterSpec, ocr_preview_binds

from tests.powermeter.ocr.conftest import FakeEngine


class FakePreview:
    """A stand-in for `PreviewServer` -- identity is all these tests care about."""


class FakeFrames:
    def latest(self) -> Frame | None:
        return None

    def wait_for_frame(self, after_sequence: int, timeout: float) -> Frame | None:
        return None

    def close(self) -> None:
        pass


def _ocr_meter(preview: object | None) -> OcrPowerMeter:
    return OcrPowerMeter(FakeFrames(), DisplayReader(FakeEngine([]), PR10), preview=preview)  # type: ignore[arg-type]


@dataclass
class FakeClock:
    current: float = 0.0

    def monotonic(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


@dataclass
class ClosableDummyMeter(DummyPowerMeter):
    """A meter that records whether/how often it was closed."""

    closes: list[int] = field(default_factory=list)

    def close(self) -> None:
        self.closes.append(1)


def _service(clock: FakeClock) -> tuple[MeterPreviewService, OcrPreviewRegistry]:
    registry = OcrPreviewRegistry()
    service = MeterPreviewService(
        registry,
        monotonic=clock.monotonic,
        sleep=lambda _seconds: None,
        start_reaper=False,
    )
    return service, registry


def test_a_meter_with_no_ocr_component_has_nothing_to_preview_and_is_closed_immediately() -> None:
    clock = FakeClock()
    service, registry = _service(clock)
    meter = ClosableDummyMeter()

    preview_id, labels = service.start(DummyPowerMeterSpec(), lambda _spec: meter)

    assert preview_id is None
    assert labels == ()
    assert meter.closes == [1]
    assert registry.labels("anything") == ()


def test_an_ocr_meter_is_registered_under_a_fresh_id_and_kept_open() -> None:
    clock = FakeClock()
    service, registry = _service(clock)
    preview = FakePreview()
    meter = _ocr_meter(preview)

    preview_id, labels = service.start(DummyPowerMeterSpec(), lambda _spec: meter)

    assert preview_id is not None
    assert preview_id.startswith("preview-")
    assert labels == ("primary",)
    assert registry.get(preview_id, "primary") is preview

    service.stop(preview_id)

    assert registry.labels(preview_id) == ()


def test_stop_closes_the_underlying_meter() -> None:
    clock = FakeClock()
    service, _registry = _service(clock)
    closed = []
    meter = _ocr_meter(FakePreview())
    meter.close = lambda: closed.append(1)  # type: ignore[method-assign]

    preview_id, _labels = service.start(DummyPowerMeterSpec(), lambda _spec: meter)
    assert preview_id is not None

    service.stop(preview_id)

    assert closed == [1]


def test_touch_and_stop_are_no_ops_for_an_unknown_id() -> None:
    clock = FakeClock()
    service, _registry = _service(clock)

    service.touch("never-started")  # must not raise
    service.stop("never-started")  # must not raise


def test_reaper_closes_previews_that_have_not_been_touched_within_the_idle_timeout() -> None:
    clock = FakeClock()
    service, registry = _service(clock)
    closed = []
    meter = _ocr_meter(FakePreview())
    meter.close = lambda: closed.append(1)  # type: ignore[method-assign]

    preview_id, _labels = service.start(DummyPowerMeterSpec(), lambda _spec: meter)
    assert preview_id is not None

    clock.advance(IDLE_TIMEOUT_SECONDS + 1)
    service._reap_once()  # noqa: SLF001 - drive one sweep directly instead of a real background thread

    assert closed == [1]
    assert registry.labels(preview_id) == ()


def test_touching_a_preview_keeps_it_alive_past_the_idle_timeout() -> None:
    clock = FakeClock()
    service, registry = _service(clock)
    closed = []
    meter = _ocr_meter(FakePreview())
    meter.close = lambda: closed.append(1)  # type: ignore[method-assign]

    preview_id, _labels = service.start(DummyPowerMeterSpec(), lambda _spec: meter)
    assert preview_id is not None

    clock.advance(IDLE_TIMEOUT_SECONDS - 1)
    service.touch(preview_id)
    clock.advance(IDLE_TIMEOUT_SECONDS - 1)
    service._reap_once()  # noqa: SLF001

    assert closed == []
    assert registry.labels(preview_id) == ("primary",)


def test_ocr_preview_binds_are_the_listen_addresses_not_the_camera_source() -> None:
    spec = OcrPowerMeterSpec(source="http://camera/stream", preview_host="127.0.0.1", preview_port=8765)

    assert ocr_preview_binds(spec) == frozenset({("127.0.0.1", 8765)})
    assert ocr_preview_binds(DummyPowerMeterSpec()) == frozenset()


def test_start_reuses_a_live_preview_with_the_same_spec() -> None:
    clock = FakeClock()
    service, registry = _service(clock)
    closed: list[int] = []
    meter = _ocr_meter(FakePreview())
    meter.close = lambda: closed.append(1)  # type: ignore[method-assign]
    spec = OcrPowerMeterSpec(source="0", preview_host="127.0.0.1", preview_port=8765)
    built: list[int] = []

    first_id, first_labels = service.start(spec, lambda _spec: built.append(1) or meter)
    second_id, second_labels = service.start(spec, lambda _spec: built.append(1) or meter)

    assert first_id == second_id
    assert first_labels == second_labels == ("primary",)
    assert built == [1]
    assert closed == []
    assert registry.labels(first_id or "") == ("primary",)


def test_borrow_reuses_a_live_preview_that_already_owns_the_port() -> None:
    clock = FakeClock()
    service, _registry = _service(clock)
    closed: list[int] = []
    meter = _ocr_meter(FakePreview())
    meter.close = lambda: closed.append(1)  # type: ignore[method-assign]
    spec = OcrPowerMeterSpec(source="0", preview_host="127.0.0.1", preview_port=8765)

    preview_id, _labels = service.start(spec, lambda _spec: meter)
    assert preview_id is not None

    borrowed = service.borrow(OcrPowerMeterSpec(source="http://other", preview_host="127.0.0.1", preview_port=8765))
    assert borrowed is not None
    borrowed.close()

    assert closed == []
    assert service.borrow(OcrPowerMeterSpec(source="0", preview_host="127.0.0.1", preview_port=8766)) is None
    assert service.borrow(DummyPowerMeterSpec()) is None
