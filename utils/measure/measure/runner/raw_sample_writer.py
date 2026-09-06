"""Append-only JSONL log of every accepted sample's P/V/I/PF, for primary and witnesses.

The LUT CSV a measurement produces only ever holds the primary's power, one value per
variation -- correct for its job, but it throws away everything that would let you check
*after the fact* whether the primary and any witness(es) actually agreed for a given
point, or whether one of them was reporting an implausible voltage/current/PF. This
writer captures that raw evidence alongside the CSV, one JSON object per line, so a
run's internal consistency can be inspected later without having to have been watching
the live composite-meter log at the time.

Deliberately a separate file rather than extra CSV columns: the CSV format is used
elsewhere (resume-from-CSV, the LUT compiler, upstream's model.json converter) and this
is diagnostic-only, additive data that none of those need to know about, plus the width
here (witness count is not fixed) doesn't fit a fixed-column CSV cleanly.

Only meaningful for a ``CompositePowerMeter``; ``RawSampleWriter.write`` is a no-op if
the reading passed in isn't one, since a bare single meter's own measurement is already
exactly what the CSV records.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from measure.controller.light.const import LutMode
    from measure.powermeter.composite import CompositeReading
    from measure.runner.light_plan import Variation

_LOGGER = logging.getLogger("measure")


class RawSampleWriter:
    """Appends one JSON line per accepted sample to a ``.raw.jsonl`` file."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - held open for the runner's lifetime

    def write(self, *, mode: LutMode, variation: Variation, reading: CompositeReading | None) -> None:
        if reading is None:
            return
        row = {
            "timestamp": time.time(),
            "mode": mode.value,
            "variation": asdict(variation),
            "primary": {
                "power": reading.primary.power,
                "voltage": reading.primary.voltage,
                "current": reading.primary.current,
                "power_factor": reading.primary.power_factor,
            },
            "witnesses": [
                {
                    "name": witness.name,
                    "power": witness.power,
                    "corrected": witness.corrected,
                    "deviation": witness.deviation,
                    "agrees": witness.agrees,
                    "error": witness.error,
                    "voltage": witness.voltage,
                    "current": witness.current,
                    "power_factor": witness.power_factor,
                }
                for witness in reading.witnesses
            ],
        }
        try:
            self._file.write(json.dumps(row) + "\n")
            self._file.flush()
        except OSError as error:
            # Diagnostic-only data; never let a write failure here abort the measurement.
            _LOGGER.warning("Could not append raw sample to %s: %s", self._path, error)

    def close(self) -> None:
        try:
            self._file.close()
        except OSError as error:
            _LOGGER.info("Could not close raw sample file %s: %s", self._path, error)
