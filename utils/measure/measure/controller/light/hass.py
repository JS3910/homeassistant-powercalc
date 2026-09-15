from collections.abc import Callable, Mapping, Sequence
import logging
import time
from typing import Any

from homeassistant_api import State
from homeassistant_api.errors import HomeassistantAPIError

from measure.const import HASS_ENTITY_GROUP_MEMBERS
from measure.controller.errors import ApiConnectionError, ControllerError
from measure.controller.hass_controller import HassControllerBase
from measure.controller.light.capabilities import (
    common_effects,
    light_info_from_attributes,
    merge_light_infos,
    mired_to_kelvin,
)
from measure.controller.light.const import LutMode
from measure.controller.light.controller import LightController, LightInfo
from measure.home_assistant import HomeAssistantManager, explain_home_assistant_error
from measure.home_assistant_entities import leaf_light_entity_ids

_LOGGER = logging.getLogger("measure")
#: How long to wait for every leaf light to report the requested on/off state.
# A member that never arrives is a hard error: sampling anyway poisons the LUT.
STATE_CONFIRM_TIMEOUT = 20.0
STATE_CONFIRM_POLL = 0.25
# HA brightness is 0-255. Some stacks report 254 for a commanded 255.
BRIGHTNESS_CONFIRM_SLACK = 2


def _command_summary(lut_mode: LutMode, on: bool, kwargs: dict[str, Any]) -> str:
    if not on:
        return "off"
    extras = [f"{key}={kwargs[key]}" for key in ("bri", "ct", "hue", "sat", "effect", "white") if key in kwargs]
    return ", ".join([f"mode={lut_mode.value}", *extras])


def _format_light_states(entity_ids: Sequence[str], last_seen: Mapping[str, str], fallback: str) -> str:
    return "; ".join(f"{entity_id}={last_seen.get(entity_id, fallback)}" for entity_id in entity_ids)


def _brightness_reached(brightness: object, wanted_bri: int) -> bool:
    if isinstance(brightness, bool) or not isinstance(brightness, (int, float, str)):
        return False
    try:
        reported = int(round(float(brightness)))
    except ValueError:
        return False
    return abs(reported - wanted_bri) <= BRIGHTNESS_CONFIRM_SLACK


def _format_seen_state(state: object, brightness: object, wanted_bri: int | None) -> str:
    seen = f"{state}, brightness={brightness}"
    if wanted_bri is not None and not _brightness_reached(brightness, wanted_bri):
        return f"{seen} (want {wanted_bri})"
    return seen


class HassLightController(HassControllerBase, LightController):
    """Drive one or more Home Assistant lights as a single measurement target.

    Several identical lights can be measured together to lift a load that is too small
    to register on its own. They then report the capabilities they have in common.
    """

    def __init__(
        self,
        home_assistant: HomeAssistantManager,
        transition_time: int,
        *,
        entity_ids: Sequence[str],
        wait: Callable[[float], None] = time.sleep,
        state_confirm_timeout: float = STATE_CONFIRM_TIMEOUT,
    ) -> None:
        if not entity_ids:
            raise ValueError("A light controller needs at least one entity")
        self._transition_time: int = transition_time
        self._wait = wait
        self._state_confirm_timeout = state_confirm_timeout
        # Every light is addressed explicitly, so the base class' single entity_id does not apply.
        self.entity_ids = list(entity_ids)
        super().__init__(home_assistant)

    def service_target(self, entity_ids: Sequence[str] | None = None) -> str | list[str]:
        """Entity target for light services: one light stays a plain ID, several become a list."""
        targets = list(entity_ids) if entity_ids is not None else self.entity_ids
        return targets[0] if len(targets) == 1 else targets

    def change_light_state(
        self,
        lut_mode: LutMode,
        on: bool = True,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        targets = self._leaf_entity_ids()
        target = self.service_target(targets)
        service = "turn_on" if on else "turn_off"
        started = time.monotonic()
        _LOGGER.info(
            "Sending light.%s to %s (%s)",
            service,
            ", ".join(targets),
            _command_summary(lut_mode, on, kwargs),
        )
        try:
            if not on:
                self.client.trigger_service("light", "turn_off", entity_id=target, isolated=True)
            else:
                if lut_mode == LutMode.HS:
                    json = self.build_hs_json_body(kwargs["bri"], kwargs["hue"], kwargs["sat"], target)
                elif lut_mode == LutMode.COLOR_TEMP:
                    json = self.build_ct_json_body(kwargs["bri"], kwargs["ct"], target)
                elif lut_mode == LutMode.EFFECT:
                    json = self.build_effect_json_body(kwargs["bri"], kwargs["effect"], target)
                elif lut_mode == LutMode.WHITE:
                    json = self.build_white_json_body(kwargs["bri"], target)
                else:
                    json = self.build_bri_json_body(kwargs["bri"], target)
                self.client.trigger_service("light", "turn_on", **json)
        except (HomeassistantAPIError, OSError) as e:
            elapsed = time.monotonic() - started
            raise ApiConnectionError(
                f"Home Assistant did not accept light.{service} for {', '.join(targets)} "
                f"after {elapsed:.1f}s: {explain_home_assistant_error(e)}"
            ) from e
        _LOGGER.info(
            "light.%s accepted for %s in %.1fs",
            service,
            ", ".join(targets),
            time.monotonic() - started,
        )
        if not on:
            _LOGGER.info("Skipping state confirm after turn_off for %s", ", ".join(targets))
            return
        if self._transition_time:
            _LOGGER.info("Waiting %.1fs for the configured light transition", self._transition_time)
        self._wait(self._transition_time)
        wanted_bri = kwargs.get("bri") if on else None
        if isinstance(wanted_bri, (int, float)):
            wanted_bri = int(wanted_bri)
        else:
            wanted_bri = None
        self._confirm_states(targets, on=on, wanted_bri=wanted_bri)

    def get_light_info(self) -> LightInfo:
        return merge_light_infos([light_info_from_attributes(state.attributes) for state in self._states()])

    def has_effect_support(self) -> bool:
        return True

    def get_effect_list(self) -> list[str]:
        return common_effects(
            [[str(effect) for effect in (state.attributes.get("effect_list") or [])] for state in self._states()],
        )

    def close(self) -> None:
        return

    def build_hs_json_body(self, bri: int, hue: int, sat: int, target: str | list[str] | None = None) -> dict[str, Any]:
        return {
            "entity_id": target if target is not None else self.service_target(),
            "transition": self._transition_time,
            "brightness": bri,
            "hs_color": [hue / 65535 * 360, sat / 255 * 100],
        }

    def build_ct_json_body(self, bri: int, ct: int, target: str | list[str] | None = None) -> dict[str, Any]:
        return {
            "entity_id": target if target is not None else self.service_target(),
            "transition": self._transition_time,
            "brightness": bri,
            "color_temp_kelvin": mired_to_kelvin(ct),
        }

    def build_bri_json_body(self, bri: int, target: str | list[str] | None = None) -> dict[str, Any]:
        return {
            "entity_id": target if target is not None else self.service_target(),
            "transition": self._transition_time,
            "brightness": bri,
        }

    def build_effect_json_body(self, bri: int, effect: str, target: str | list[str] | None = None) -> dict[str, Any]:
        return {
            "entity_id": target if target is not None else self.service_target(),
            "effect": effect,
            "brightness": bri,
        }

    def build_white_json_body(self, bri: int, target: str | list[str] | None = None) -> dict[str, Any]:
        return {
            "entity_id": target if target is not None else self.service_target(),
            "white": bri,
        }

    def _leaf_entity_ids(self) -> list[str]:
        members_by_id: dict[str, list[str]] = {}
        pending = list(self.entity_ids)
        seen: set[str] = set()
        while pending:
            entity_id = pending.pop()
            if entity_id in seen:
                continue
            seen.add(entity_id)
            try:
                state = self.client.get_state(entity_id=entity_id)
            except (HomeassistantAPIError, OSError, TypeError, AttributeError):
                members_by_id[entity_id] = []
                continue
            members = state.attributes.get(HASS_ENTITY_GROUP_MEMBERS)
            if isinstance(members, list) and members:
                child_ids = [str(member) for member in members]
                members_by_id[entity_id] = child_ids
                pending.extend(child_ids)
            else:
                members_by_id[entity_id] = []
        return leaf_light_entity_ids(self.entity_ids, members_by_id)

    def _confirm_states(self, targets: Sequence[str], *, on: bool, wanted_bri: int | None = None) -> None:
        if self._state_confirm_timeout <= 0 or not targets:
            return
        wanted = "on" if on else "off"
        brightness_note = f" at brightness {wanted_bri}" if wanted_bri is not None else ""
        _LOGGER.info(
            "Waiting for %s to report %s%s (timeout %.1fs)",
            ", ".join(targets),
            wanted,
            brightness_note,
            self._state_confirm_timeout,
        )
        deadline = time.time() + self._state_confirm_timeout
        pending = list(targets)
        last_seen: dict[str, str] = {}
        started = time.monotonic()
        last_report = started
        while pending and time.time() < deadline:
            pending = self._pending_after_poll(
                pending,
                wanted=wanted,
                on=on,
                wanted_bri=wanted_bri,
                last_seen=last_seen,
            )
            now = time.monotonic()
            if pending and now - last_report >= 5:
                _LOGGER.warning(
                    "Still waiting for lights after %.1fs: %s",
                    now - started,
                    _format_light_states(pending, last_seen, "unknown"),
                )
                last_report = now
            if pending:
                self._wait(min(STATE_CONFIRM_POLL, max(0.0, deadline - time.time())))
        if pending:
            raise ControllerError(
                f"Lights did not reach {wanted} after {self._state_confirm_timeout:.1f}s: "
                f"{_format_light_states(pending, last_seen, 'unknown')}. "
                "Measurement stopped so a stuck light cannot poison the LUT.",
            )
        _LOGGER.info(
            "All lights reported %s in %.1fs: %s",
            wanted,
            time.monotonic() - started,
            _format_light_states(targets, last_seen, wanted),
        )

    def _pending_after_poll(
        self,
        pending: Sequence[str],
        *,
        wanted: str,
        on: bool,
        wanted_bri: int | None,
        last_seen: dict[str, str],
    ) -> list[str]:
        still_waiting: list[str] = []
        for entity_id in pending:
            try:
                state = self.client.get_state(entity_id=entity_id)
            except (HomeassistantAPIError, OSError, TypeError, AttributeError) as error:
                last_seen[entity_id] = f"unreachable ({error})"
                still_waiting.append(entity_id)
                continue
            brightness = state.attributes.get("brightness")
            last_seen[entity_id] = _format_seen_state(state.state, brightness, wanted_bri)
            if str(state.state).casefold() != wanted or (on and brightness is None):
                still_waiting.append(entity_id)
                continue
            if on and wanted_bri is not None and not _brightness_reached(brightness, wanted_bri):
                still_waiting.append(entity_id)
        return still_waiting

    def _states(self) -> list[State]:
        return [self.client.get_state(entity_id=entity_id) for entity_id in self.entity_ids]
