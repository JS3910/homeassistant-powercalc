import re

from measure.powermeter.ocr.layout import LAYOUTS, PR10, DisplayLayout, FieldPattern, reinsert_decimal
import pytest


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Power4.60w", "4.60"),
        ("Pawer406.7v", "406.7"),
        ("Power1178w", "1178"),
        ("Power 1178", "1178"),
        ("PowerO.71W", "0.71"),
        ("Power0.00Ww", "0.00"),  # doubled unit glyph
        ("PawerO.00wW", "0.00"),
        ("PF0.998", None),  # the PF row is not the power row
        ("Pow44.60Www", None),
        ("Power4.60w1", None),
        ("", None),
    ],
)
def test_power_parsing(text: str, expected: str | None) -> None:
    assert PR10.parse("power", text) == expected


@pytest.mark.parametrize(
    "text, voltage, current",
    [
        ("V233.1vC0.055A", "233.1", "0.055"),
        ("Ve233.5Em0.054A", "233.5", "0.054"),
        ("V228.15.194A", "228.1", "5.194"),  # voltage and current run together
        ("228.1vC5.191A", "228.1", "5.191"),  # V label not read at all
        ("V2281V5.191A", "228.1", "5.191"),  # dropped decimal point, reinserted
        ("Va233.1VC0.009", "233.1", "0.009"),  # unit glyph missing
        ("V233.1V055A", "233.1", None),  # a digit missing from the current
        ("V2.4vC0.000A", None, "0.000"),  # a digit missing from the voltage
        # No load: the recogniser collapses the run of zeros in 0.000A
        ("Va236.7VC0.0A", "236.7", "0.000"),
        ("Vam236.7V00A", "236.7", "0.000"),
        ("Vamm236.80C0A", "236.8", "0.000"),
        ("Vimm236.7VEm000A", "236.7", "0.000"),
        ("Va236.8V0.00A", "236.8", "0.000"),
        ("Va235Cu0.000A", None, "0.000"),  # voltage without its decimal
        ("V236.80A", "236.8", None),  # the current is gone, its zeros are the voltage's
        ("V236.8vC5.0A", "236.8", None),  # 5.000A collapsed: not a zero run
        ("V236.8vC0.50A", "236.8", None),  # 0.500A collapsed: not a zero run
        ("V236.8vC0.0", "236.8", None),  # a zero run needs its unit glyph
        ("Wa2-00", None, None),
    ],
)
def test_voltage_and_current_parsing(text: str, voltage: str | None, current: str | None) -> None:
    assert PR10.parse("voltage", text) == voltage
    assert PR10.parse("current", text) == current


@pytest.mark.parametrize(
    "text, expected",
    [
        ("PF0.354", "0.354"),
        ("PFO.354", "0.354"),
        ("PF0995", "0.995"),
        ("PF0.995Fr50.0Hz", "0.995"),  # merged with the neighbouring frequency row
        ("PF.000", None),
        ("PF0.0", "0.000"),  # collapsed zero run at no load
        ("PF00", "0.000"),
        ("PF0", "0.000"),
        ("PF0.99", None),
        ("Power4.60w", None),
    ],
)
def test_pf_parsing(text: str, expected: str | None) -> None:
    assert PR10.parse("pf", text) == expected


def test_fields_in_row_by_label() -> None:
    assert PR10.fields_in("Power4.60w") == {"power"}
    assert PR10.fields_in("V233.1vC0.055A") == {"voltage", "current"}
    assert PR10.fields_in("233.0v C0.055A") == {"voltage", "current"}
    assert PR10.fields_in("Veg") == {"voltage", "current"}
    assert PR10.fields_in("PF0.354") == {"pf"}
    assert PR10.fields_in("Real-Time Parameter") == set()
    assert PR10.fields_in("PR10 Power Recorder") == {"power"}  # a label-only match; the value does not parse


@pytest.mark.parametrize(
    "value, decimals, expected",
    [
        ("5194", 3, "5.194"),
        ("0055", 3, "0.055"),
        ("2281", 1, "228.1"),
        ("5.194", 3, "5.194"),
        ("5.19", 3, None),
        ("055", 3, None),
        (".194", 3, None),
        ("0.0", 3, "0.000"),
        ("00", 3, "0.000"),
        ("0", 1, "0.0"),
        ("", 3, None),
    ],
)
def test_reinsert_decimal(value: str, decimals: int, expected: str | None) -> None:
    assert reinsert_decimal(value, decimals) == expected


def test_fixed_decimal_field_rejects_a_value_with_the_wrong_number_of_decimals() -> None:
    layout = DisplayLayout(
        name="two-decimals",
        fields={"energy": FieldPattern(label=re.compile(r"^E"), value=re.compile(r"^E(\d+\.?\d*)"), decimals=2)},
    )
    assert layout.parse("energy", "E12.34") == "12.34"
    assert layout.parse("energy", "E1234") == "12.34"
    assert layout.parse("energy", "E12.3") is None


def test_layout_registry_and_row_order() -> None:
    assert LAYOUTS["pr10"] is PR10
    assert PR10.row_order == (("power",), ("voltage", "current"), ("pf",))
    assert set(PR10.fields) == {"power", "voltage", "current", "pf"}
