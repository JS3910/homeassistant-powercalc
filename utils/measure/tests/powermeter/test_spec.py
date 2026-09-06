from measure.powermeter.const import WitnessPosition
from measure.powermeter.spec import ManualPowerMeterSpec, WitnessSpec
import pytest


def _witness(**kwargs: object) -> WitnessSpec:
    return WitnessSpec(meter=ManualPowerMeterSpec(), **kwargs)


class TestOffsetSignValidation:
    """``offset_w`` is a magnitude for BEFORE_PRIMARY/AFTER_PRIMARY; only NONE (a fixed
    calibration bias, unrelated to wiring) may be negative."""

    def test_none_position_accepts_a_negative_offset(self) -> None:
        witness = _witness(position=WitnessPosition.NONE, offset_w=-0.44)
        assert witness.offset_w == -0.44

    def test_before_primary_rejects_a_negative_offset(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            _witness(position=WitnessPosition.BEFORE_PRIMARY, offset_w=-2.45)

    def test_after_primary_rejects_a_negative_offset(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            _witness(position=WitnessPosition.AFTER_PRIMARY, offset_w=-2.45)

    def test_before_primary_accepts_a_positive_offset(self) -> None:
        witness = _witness(position=WitnessPosition.BEFORE_PRIMARY, offset_w=2.45)
        assert witness.offset_w == 2.45

    def test_after_primary_accepts_a_positive_offset(self) -> None:
        witness = _witness(position=WitnessPosition.AFTER_PRIMARY, offset_w=2.45)
        assert witness.offset_w == 2.45

    def test_zero_is_always_accepted_regardless_of_position(self) -> None:
        for position in WitnessPosition:
            assert _witness(position=position, offset_w=0.0).offset_w == 0.0


class TestSignedOffsetW:
    """The sign ``CompositePowerMeter``'s comparison (and, for AFTER_PRIMARY, the primary
    correction) actually needs, derived from position + the entered magnitude."""

    def test_none_position_is_unchanged(self) -> None:
        assert _witness(position=WitnessPosition.NONE, offset_w=-0.44).signed_offset_w == -0.44
        assert _witness(position=WitnessPosition.NONE, offset_w=0.44).signed_offset_w == 0.44

    def test_before_primary_is_unchanged(self) -> None:
        assert _witness(position=WitnessPosition.BEFORE_PRIMARY, offset_w=2.45).signed_offset_w == 2.45

    def test_after_primary_is_negated(self) -> None:
        assert _witness(position=WitnessPosition.AFTER_PRIMARY, offset_w=2.45).signed_offset_w == -2.45

    def test_default_position_is_none(self) -> None:
        assert _witness().position == WitnessPosition.NONE
