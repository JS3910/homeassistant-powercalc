from collections.abc import Mapping, Sequence
import math
from typing import Any

from measure.controller.light.const import (
    HASS_BRIGHTNESS_COMPATIBLE_COLOR_MODES,
    HASS_HS_COMPATIBLE_COLOR_MODES,
    MAX_MIRED,
    MIN_MIRED,
    LutMode,
)
from measure.controller.light.controller import LightInfo


def merge_light_infos(infos: Sequence[LightInfo]) -> LightInfo:
    """Reduce several lights to the color temperature range they all support."""

    if not infos:
        raise ValueError("Cannot merge an empty set of light infos")
    return LightInfo(
        "unknown",
        min_mired=max(info.min_mired for info in infos),
        max_mired=min(info.max_mired for info in infos),
    )


def common_effects(effect_lists: Sequence[Sequence[str]]) -> list[str]:
    """Effects every light offers, in the order the first light lists them."""

    if not effect_lists:
        return []
    first, *rest = effect_lists
    return [effect for effect in first if all(effect in other for other in rest)]


def light_info_from_attributes(attributes: Mapping[str, Any]) -> LightInfo:
    """Translate Home Assistant light attributes into runner capabilities."""

    min_mired = MIN_MIRED
    if kelvin := attributes.get("max_color_temp_kelvin"):
        min_mired = kelvin_to_mired(float(kelvin))
    max_mired = MAX_MIRED
    if kelvin := attributes.get("min_color_temp_kelvin"):
        max_mired = kelvin_to_mired(float(kelvin))
    return LightInfo("unknown", min_mired, max_mired)


def _hass_color_modes(attributes: Mapping[str, Any]) -> set[str]:
    """``supported_color_modes`` as lowercase HA color-mode strings.

    The websocket client sometimes yields ``ColorMode`` enums or ``ColorMode.color_temp``
    strings instead of the bare ``color_temp`` / ``xy`` values REST returns.
    """

    raw = attributes.get("supported_color_modes") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    modes: set[str] = set()
    for item in raw:
        value = getattr(item, "value", item)
        text = str(value).strip()
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        if text:
            modes.add(text.lower())
    return modes


def supported_light_modes(attributes: Mapping[str, Any]) -> list[LutMode]:
    """Translate a light's ``supported_color_modes`` into the LUT modes it can be profiled in.

    Per the Home Assistant light entity model (developers.home-assistant.io/docs/core/entity/light,
    confirmed current as of Core 2026.8), ``ColorMode.BRIGHTNESS`` is a dimmable-*only* mode: it
    "must be the only supported mode if supported by the light". Every other non-onoff mode
    (``color_temp``, ``hs``, ``rgb``, ``rgbw``, ``rgbww``, ``white``, ``xy``) already implies
    brightness control on top of its own feature — a light reporting ``["color_temp", "hs"]`` is
    just as brightness-adjustable as one reporting only ``["brightness"]``, it simply never lists
    the literal ``"brightness"`` string because a more specific mode already covers it. Gating on
    that literal string previously hid the Brightness measurement option for any light with color
    or color-temperature support.

    Color temperature is also inferred from ``min/max_color_temp_kelvin``: some integrations
    expose a kelvin range (and even report ``color_mode: color_temp``) while listing only
    ``xy`` in ``supported_color_modes``.
    """
    values = _hass_color_modes(attributes)
    modes: list[LutMode] = []
    if values & HASS_BRIGHTNESS_COMPATIBLE_COLOR_MODES:
        modes.append(LutMode.BRIGHTNESS)
    if "color_temp" in values or attributes.get("min_color_temp_kelvin") or attributes.get(
        "max_color_temp_kelvin",
    ):
        modes.append(LutMode.COLOR_TEMP)
    if values & HASS_HS_COMPATIBLE_COLOR_MODES:
        modes.append(LutMode.HS)
    if attributes.get("effect_list"):
        modes.append(LutMode.EFFECT)
    return modes


def kelvin_to_mired(kelvin_temperature: float) -> int:
    return math.floor(1_000_000 / kelvin_temperature)


def mired_to_kelvin(mired_temperature: float) -> int:
    return math.floor(1_000_000 / mired_temperature)


def kelvin_range_to_mired(min_kelvin: int, max_kelvin: int) -> tuple[int, int]:
    """User Kelvin clamp (warm..cool) as inclusive mired (cool..warm)."""

    cool = kelvin_to_mired(max(min_kelvin, max_kelvin))
    warm = kelvin_to_mired(min(min_kelvin, max_kelvin))
    return min(cool, warm), max(cool, warm)


def clamp_mired_range(light_info: LightInfo, min_kelvin: int, max_kelvin: int) -> tuple[int, int]:
    """Intersect the light's mired span with a user Kelvin window."""

    light_cool = round(light_info.min_mired)
    light_warm = round(light_info.max_mired)
    if light_cool > light_warm:
        light_cool, light_warm = light_warm, light_cool
    user_cool, user_warm = kelvin_range_to_mired(min_kelvin, max_kelvin)
    cool = max(light_cool, user_cool)
    warm = min(light_warm, user_warm)
    if cool <= warm:
        return cool, warm
    if user_warm < light_cool:
        return light_cool, light_cool
    return light_warm, light_warm
