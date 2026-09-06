from measure.controller.light.capabilities import supported_light_modes
from measure.controller.light.const import LutMode
import pytest


@pytest.mark.parametrize(
    "supported_color_modes, effects, expected",
    [
        # color_temp and hs each imply brightness on their own (a light never reports the literal
        # "brightness" string alongside a more specific mode) — see capabilities.py's docstring.
        (["color_temp"], ["candle"], [LutMode.BRIGHTNESS, LutMode.COLOR_TEMP, LutMode.EFFECT]),
        (
            ["color_temp", "xy"],
            ["candle"],
            [LutMode.BRIGHTNESS, LutMode.COLOR_TEMP, LutMode.HS, LutMode.EFFECT],
        ),
        (["rgb"], [], [LutMode.BRIGHTNESS, LutMode.HS]),
        (["rgbw"], [], [LutMode.BRIGHTNESS, LutMode.HS]),
        (["rgbww"], [], [LutMode.BRIGHTNESS, LutMode.HS]),
        (["white", "hs"], [], [LutMode.BRIGHTNESS, LutMode.HS]),
        (["brightness"], [], [LutMode.BRIGHTNESS]),
        (["onoff"], [], []),
        ([], [], []),
    ],
)
def test_supported_light_modes_normalizes_home_assistant_color_modes(
    supported_color_modes: list[str],
    effects: list[str],
    expected: list[LutMode],
) -> None:
    attributes = {
        "supported_color_modes": supported_color_modes,
        "effect_list": effects,
    }

    assert supported_light_modes(attributes) == expected
