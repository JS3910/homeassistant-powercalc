from unittest.mock import MagicMock

from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightInfo
from measure.runner.light_setup import set_light_to_maximum_brightness


def test_warmup_sends_two_commands_and_does_not_add_settle_sleeps() -> None:
    controller = MagicMock()
    waits: list[float] = []
    phases: list[str] = []

    set_light_to_maximum_brightness(
        controller,
        LightInfo("test"),
        LutMode.HS,
        checkpoint=lambda: None,
        phase=lambda message, **_: phases.append(message),
    )

    assert controller.change_light_state.call_count == 2
    controller.change_light_state.assert_called_with(LutMode.HS, on=True, bri=255, hue=0, sat=1)
    assert waits == []
    assert phases == [
        "Full-brightness warm-up (1 of 2): sending command",
        "Full-brightness warm-up (2 of 2): sending command",
    ]


def test_warmup_uses_the_supplied_send_callable() -> None:
    controller = MagicMock()
    sent: list[tuple[object, ...]] = []

    def send(*args: object, **kwargs: object) -> None:
        sent.append(args)

    set_light_to_maximum_brightness(
        controller,
        LightInfo("test"),
        LutMode.BRIGHTNESS,
        send=send,
    )

    assert controller.change_light_state.call_count == 0
    assert len(sent) == 2
    assert sent[0][0] == LutMode.BRIGHTNESS
