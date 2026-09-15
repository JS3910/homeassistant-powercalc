from __future__ import annotations

from measure.ha_app.ocr_preview import OcrPreviewHandle, OcrPreviewRegistry, find_ocr_previews
from measure.powermeter.composite import CompositePowerMeter, Witness
from measure.powermeter.dummy import DummyPowerMeter
from measure.powermeter.ocr.capture import Frame
from measure.powermeter.ocr.layout import PR10
from measure.powermeter.ocr.meter import OcrPowerMeter
from measure.powermeter.ocr.reader import DisplayReader

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


def test_a_bare_non_ocr_meter_has_no_previews() -> None:
    assert find_ocr_previews(DummyPowerMeter()) == ()


def test_a_bare_ocr_meter_with_no_preview_configured_has_no_previews() -> None:
    assert find_ocr_previews(_ocr_meter(None)) == ()


def test_a_bare_ocr_meter_is_labelled_primary() -> None:
    preview = FakePreview()

    handles = find_ocr_previews(_ocr_meter(preview))

    assert handles == (OcrPreviewHandle(label="primary", preview=preview),)  # type: ignore[arg-type]


def test_an_ocr_primary_inside_a_composite_meter_is_still_labelled_primary() -> None:
    preview = FakePreview()
    composite = CompositePowerMeter(
        _ocr_meter(preview),
        [Witness(name="shelly", meter=DummyPowerMeter())],
    )

    handles = find_ocr_previews(composite)

    assert handles == (OcrPreviewHandle(label="primary", preview=preview),)  # type: ignore[arg-type]


def test_an_ocr_witness_inside_a_composite_meter_is_labelled_by_its_witness_name() -> None:
    preview = FakePreview()
    composite = CompositePowerMeter(
        DummyPowerMeter(),
        [Witness(name="pr10", meter=_ocr_meter(preview))],
    )

    handles = find_ocr_previews(composite)

    assert handles == (OcrPreviewHandle(label="pr10", preview=preview),)  # type: ignore[arg-type]


def test_a_non_ocr_primary_and_witness_together_have_no_previews() -> None:
    composite = CompositePowerMeter(
        DummyPowerMeter(),
        [Witness(name="shelly", meter=DummyPowerMeter())],
    )

    assert find_ocr_previews(composite) == ()


def test_registry_round_trips_registered_previews_by_label() -> None:
    registry = OcrPreviewRegistry()
    preview = FakePreview()
    registry.register("session-1", (OcrPreviewHandle(label="primary", preview=preview),))  # type: ignore[arg-type]

    assert registry.get("session-1", "primary") is preview
    assert registry.labels("session-1") == ("primary",)


def test_registry_returns_none_for_an_unknown_session_or_label() -> None:
    registry = OcrPreviewRegistry()

    assert registry.get("missing", "primary") is None
    assert registry.labels("missing") == ()

    registry.register("session-1", (OcrPreviewHandle(label="primary", preview=FakePreview()),))  # type: ignore[arg-type]
    assert registry.get("session-1", "witness") is None


def test_registry_registering_an_empty_tuple_is_a_no_op() -> None:
    registry = OcrPreviewRegistry()

    registry.register("session-1", ())

    assert registry.labels("session-1") == ()


def test_registry_unregister_forgets_a_sessions_previews() -> None:
    registry = OcrPreviewRegistry()
    registry.register("session-1", (OcrPreviewHandle(label="primary", preview=FakePreview()),))  # type: ignore[arg-type]

    registry.unregister("session-1")

    assert registry.labels("session-1") == ()
    assert registry.get("session-1", "primary") is None
