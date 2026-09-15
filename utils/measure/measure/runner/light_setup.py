from collections.abc import Callable
import logging
import time

from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightController, LightInfo

_LOGGER = logging.getLogger("measure")


def set_light_to_maximum_brightness(
    controller: LightController,
    light_info: LightInfo,
    mode: LutMode,
    *,
    checkpoint: Callable[[], None] | None = None,
    phase: Callable[..., None] | None = None,
    send: Callable[..., None] | None = None,
) -> None:
    """Send maximum brightness twice for lights that drop the first on-command (#2598).

    Each ``change_light_state`` already waits until the lights report on. A further
    measurement-settle sleep here only stacked on top of that — the first real
    sample still does its own settle / ``sleep_initial``.
    """

    kwargs: dict[str, int] = {"bri": 255}
    if mode == LutMode.HS:
        kwargs.update(hue=0, sat=1)
    elif mode == LutMode.COLOR_TEMP:
        kwargs["ct"] = light_info.min_mired
    else:
        mode = LutMode.BRIGHTNESS

    extra = "".join(f" {key}={value}" for key, value in kwargs.items() if key != "bri")
    _LOGGER.info(
        "Full-brightness warm-up: two identical max-brightness commands (bri=255%s) "
        "back to back — some lights drop the first on-command after being off",
        extra,
    )
    for index in range(2):
        if checkpoint is not None:
            checkpoint()
        step = index + 1
        if phase is not None:
            phase(f"Full-brightness warm-up ({step} of 2): sending command")
        started = time.monotonic()
        _LOGGER.info("Full-brightness warm-up %d/2: sending command", step)
        (send or controller.change_light_state)(mode, on=True, **kwargs)
        _LOGGER.info(
            "Full-brightness warm-up %d/2: command returned in %.1fs",
            step,
            time.monotonic() - started,
        )
