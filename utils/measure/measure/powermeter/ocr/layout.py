"""Describes what a power meter's display shows and how to parse the text read from it.

A layout is data, not code: which fields exist, how their rows are recognised from the
text the OCR engine reads, and how a value is normalised. Adding another meter means
adding another ``DisplayLayout`` instance, not touching the reader.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import re

# Glyphs the recogniser confuses with digits in the value part of a row.
_DIGIT_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "S": "5", "B": "8"})


@dataclass(frozen=True)
class FieldPattern:
    """How one field appears in a recognised row of display text.

    ``label`` must match the (space-stripped) text of the row for the row to be
    considered to contain this field; ``value`` extracts the number. ``decimals`` is the
    fixed number of decimals the display uses for the field, so a dropped decimal point
    can be reinserted, or ``None`` when the display floats the point.
    """

    label: re.Pattern[str]
    value: re.Pattern[str]
    decimals: int | None = None
    normalise: Callable[[str], str] | None = None


@dataclass(frozen=True)
class DisplayLayout:
    """Fields of a display and the vertical order in which their rows appear."""

    name: str
    fields: dict[str, FieldPattern]
    row_order: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    """Fields grouped per display row, top to bottom; fields in one group share a row."""

    def parse(self, name: str, text: str) -> str | None:
        """Extract a field value from a recognised row, or ``None`` if it does not parse."""
        pattern = self.fields[name]
        compact = text.replace(" ", "")
        if not pattern.label.search(compact):
            return None
        if pattern.normalise is not None:
            compact = pattern.normalise(compact)
        match = pattern.value.search(compact)
        if match is None:
            return None
        value = next(group for group in match.groups() if group is not None)
        if pattern.decimals is not None:
            return reinsert_decimal(value, pattern.decimals)
        return value

    def fields_in(self, text: str) -> set[str]:
        """Which fields a recognised row of text carries, judged by their labels."""
        compact = text.replace(" ", "")
        return {name for name, pattern in self.fields.items() if pattern.label.search(compact)}


def reinsert_decimal(value: str, decimals: int) -> str | None:
    """Put the decimal point back into a fixed-format value whose point was not read.

    ``"5194"`` with 3 decimals becomes ``"5.194"``. A value that already has a point is
    returned unchanged when it has the expected number of decimals and rejected
    otherwise; a value without a point but too few digits is rejected as well.

    The one exception is a value made of zeros only. The recogniser's CTC decoding
    merges a run of identical characters, so ``0.000`` is read as ``0.0`` or ``00``;
    since every digit is a zero the value is unambiguous and is expanded back.
    """
    if value and set(value) <= {"0", "."}:
        return f"0.{'0' * decimals}"
    if "." in value:
        whole, _, fraction = value.partition(".")
        return value if len(fraction) == decimals and whole else None
    if len(value) <= decimals:
        return None
    return f"{value[:-decimals]}.{value[-decimals:]}"


def _digits(text: str) -> str:
    """Translate digit look-alikes after the label.

    The label is the first character and the lower-case letters following it ("Power",
    "Pawer", "Ve"); an upper-case look-alike right after it ("PowerO.71") is a digit.
    """
    index = 1
    while index < len(text) and text[index].isalpha() and text[index].islower():
        index += 1
    return text[:index] + text[index:].translate(_DIGIT_LOOKALIKES)


PR10 = DisplayLayout(
    name="pr10",
    fields={
        # "Power4.60w", "Pawer406.7v", "Power0.00Ww": label letters, a floating-point value,
        # a unit glyph the recogniser renders as w or v (sometimes twice, or not at all),
        # nothing after it.
        "power": FieldPattern(
            label=re.compile(r"^P(?!F)"),
            value=re.compile(r"^P[a-zA-Z]*?(\d+\.?\d*)[wWvV]{0,2}$"),
            normalise=_digits,
        ),
        # "V233.1vC0.055A", "Ve233.5Em0.054A", "V228.15.194A", "228.1vC5.191A": voltage and
        # current share a row. The label letters are unreliable (the leading V is often
        # not read at all), so the row is recognised by its shape: three-plus-one digits
        # with a v after them, and a value with three decimals and an A at the end.
        # At no load the display shows 0.000A, which the recogniser collapses to "0.0A",
        # "00A" or "0A" (see reinsert_decimal); that zero run is only taken when it is
        # not the tail of a longer number and the A is present.
        "voltage": FieldPattern(
            label=re.compile(r"^V|^\d{3}\.\d[vV]|\.\d{3}A$"),
            value=re.compile(r"^[a-zA-Z]*(\d{3}\.?\d)"),
            decimals=1,
            normalise=_digits,
        ),
        "current": FieldPattern(
            label=re.compile(r"^V|^\d{3}\.\d[vV]|\.\d{3}A$"),
            value=re.compile(r"(\d\.?\d{3})[aA]?$|(?<![\d.])(0\.?0{0,3})[aA]$"),
            decimals=3,
            normalise=_digits,
        ),
        "pf": FieldPattern(
            label=re.compile(r"^PF"),
            # "PF0.995", or "PF0.995Fr50.0Hz" when the row is read together with its
            # neighbour; "PF0.0" is the collapsed reading of PF0.000 at no load.
            value=re.compile(r"^PF(\d\.?\d{3})|^PF(0\.?0{0,3})$"),
            decimals=3,
            normalise=_digits,
        ),
    },
    row_order=(("power",), ("voltage", "current"), ("pf",)),
)

LAYOUTS: dict[str, DisplayLayout] = {PR10.name: PR10}
