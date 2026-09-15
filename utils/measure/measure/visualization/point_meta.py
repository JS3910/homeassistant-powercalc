"""Stable identities and tooltip stats for light LUT plot points."""

from __future__ import annotations

from dataclasses import dataclass

from measure.controller.light.const import LutMode
from measure.runner.light_plan import ColorTempVariation, EffectVariation, HsVariation, Variation


@dataclass(frozen=True, slots=True)
class PlotStat:
    label: str
    value: str

_MODE_NAMES = {mode.value for mode in LutMode}


def variation_point_id(mode: LutMode, variation: Variation) -> str:
    if isinstance(variation, ColorTempVariation):
        return f"{mode.value}:{variation.bri}:{variation.ct}"
    if isinstance(variation, HsVariation):
        return f"{mode.value}:{variation.bri}:{variation.hue}:{variation.sat}"
    if isinstance(variation, EffectVariation):
        return f"{mode.value}:{variation.bri}:{variation.effect}"
    return f"{mode.value}:{variation.bri}"


def variation_rail_id(mode: LutMode, variation: Variation) -> str:
    if isinstance(variation, ColorTempVariation):
        return f"{mode.value}:{variation.ct}"
    if isinstance(variation, HsVariation):
        return f"{mode.value}:{variation.hue}:{variation.sat}"
    if isinstance(variation, EffectVariation):
        return f"{mode.value}:{variation.effect}"
    return mode.value


def parse_point_id(point_id: str) -> tuple[LutMode, Variation]:
    mode_name, rest = point_id.split(":", 1)
    if mode_name not in _MODE_NAMES:
        raise ValueError(f"Unknown plot point mode: {mode_name}")
    mode = LutMode(mode_name)
    if mode is LutMode.BRIGHTNESS:
        return mode, Variation(bri=int(rest))
    if mode is LutMode.COLOR_TEMP:
        brightness, mired = rest.split(":")
        return mode, ColorTempVariation(bri=int(brightness), ct=int(mired))
    if mode is LutMode.HS:
        brightness, hue, sat = rest.split(":")
        return mode, HsVariation(bri=int(brightness), hue=int(hue), sat=int(sat))
    brightness, effect = rest.split(":", 1)
    return mode, EffectVariation(effect=effect, bri=int(brightness))


def point_stats(mode: LutMode, variation: Variation, watt: float) -> tuple[PlotStat, ...]:
    stats = [
        PlotStat("Power", _format_watts(watt)),
        PlotStat("Brightness", f"{round(variation.bri / 255 * 100)}% ({variation.bri})"),
    ]
    if isinstance(variation, ColorTempVariation) and variation.ct > 0:
        stats.append(PlotStat("Color temperature", f"{round(1_000_000 / variation.ct)} K"))
        stats.append(PlotStat("Mired", str(variation.ct)))
    if isinstance(variation, HsVariation):
        hue_deg = variation.hue / 65535 * 360
        sat_pct = variation.sat / 255 * 100
        stats.append(PlotStat("Hue", f"{hue_deg:.1f}° ({variation.hue})"))
        stats.append(PlotStat("Saturation", f"{sat_pct:.1f}% ({variation.sat})"))
    if isinstance(variation, EffectVariation):
        stats.append(PlotStat("Effect", variation.effect))
    if mode is LutMode.BRIGHTNESS and type(variation) is Variation:
        pass
    return tuple(stats)


def _format_watts(watt: float) -> str:
    if abs(watt) >= 10:
        return f"{watt:.2f} W"
    if abs(watt) >= 1:
        return f"{watt:.3f} W"
    return f"{watt:.4f} W"
