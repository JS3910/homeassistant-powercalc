from enum import StrEnum

MIN_MIRED = 150
MAX_MIRED = 500
HASS_HS_COMPATIBLE_COLOR_MODES = frozenset({"hs", "xy", "rgb", "rgbw", "rgbww"})
#: Every ``supported_color_modes`` value other than "onoff"/"unknown" implies brightness control
#: (see developers.home-assistant.io/docs/core/entity/light) — "brightness" itself is only ever
#: reported when a light supports *no* color/color-temperature mode at all.
HASS_BRIGHTNESS_COMPATIBLE_COLOR_MODES = frozenset(
    {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"},
)
DEFAULT_LIGHT_TRANSITION_TIME = 0


class LutMode(StrEnum):
    HS = "hs"
    COLOR_TEMP = "color_temp"
    BRIGHTNESS = "brightness"
    EFFECT = "effect"
    WHITE = "white"


class LightControllerType(StrEnum):
    DUMMY = "dummy"
    HASS = "hass"
    HUE = "hue"
