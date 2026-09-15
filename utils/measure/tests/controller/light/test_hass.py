from unittest.mock import MagicMock

from homeassistant_api import State
from homeassistant_api.errors import HomeassistantAPIError
from measure.controller.errors import ApiConnectionError, ControllerError
from measure.controller.light.const import MAX_MIRED, MIN_MIRED, LutMode
from measure.controller.light.hass import HassLightController
from measure.home_assistant import HomeAssistantManager
import pytest


@pytest.mark.parametrize(
    "attributes,min_mired,max_mired",
    [
        (
            {"min_color_temp_kelvin": 2202, "max_color_temp_kelvin": 6535},
            153,
            454,
        ),
        (
            {},
            MIN_MIRED,
            MAX_MIRED,
        ),
    ],
)
def test_get_light_info(attributes: dict[str, int], min_mired: int, max_mired: int) -> None:
    mocked_state = State(
        entity_id="light.test",
        state="on",
        attributes=attributes,
    )
    client = _mock_client()
    client.get_state.return_value = mocked_state
    light_info = _get_instance(client).get_light_info()
    assert light_info.get_min_mired() == min_mired
    assert light_info.get_max_mired() == max_mired


def test_effect_list() -> None:
    mocked_state = State(
        entity_id="light.test",
        state="on",
        attributes={"effect_list": ["A", "B", "C"]},
    )
    client = _mock_client()
    client.get_state.return_value = mocked_state
    assert _get_instance(client).get_effect_list() == ["A", "B", "C"]


def test_effect_list_handles_null_value() -> None:
    mocked_state = State(
        entity_id="light.test",
        state="on",
        attributes={"effect_list": None},
    )
    client = _mock_client()
    client.get_state.return_value = mocked_state

    assert _get_instance(client).get_effect_list() == []


def test_has_effect_support() -> None:
    hass_controller = _get_instance()
    assert hass_controller.has_effect_support()


@pytest.mark.parametrize(
    "mode,call_kwargs,trigger_service_body",
    [
        (
            LutMode.BRIGHTNESS,
            {"bri": 100},
            {"brightness": 100, "transition": 0},
        ),
        (
            LutMode.COLOR_TEMP,
            {"bri": 100, "ct": 100},
            {"brightness": 100, "color_temp_kelvin": 10000, "transition": 0},
        ),
        (
            LutMode.HS,
            {"bri": 100, "hue": 100, "sat": 100},
            {"brightness": 100, "hs_color": [0.5493247882810712, 39.21568627450981], "transition": 0},
        ),
        (
            LutMode.EFFECT,
            {"bri": 100, "effect": "A"},
            {"brightness": 100, "effect": "A"},
        ),
    ],
)
def test_change_light_state(mode: LutMode, call_kwargs: dict, trigger_service_body: dict) -> None:
    client = _mock_client()
    _get_instance(client).change_light_state(mode, on=True, **call_kwargs)
    client.trigger_service.assert_called_once_with("light", "turn_on", entity_id="light.test", **trigger_service_body)


def test_group_is_expanded_and_commands_go_to_members() -> None:
    client = _mock_client()
    states = {
        "light.measure": State(
            entity_id="light.measure",
            state="on",
            attributes={"entity_id": ["light.one", "light.two", "light.three"]},
        ),
        "light.one": State(entity_id="light.one", state="off", attributes={}),
        "light.two": State(entity_id="light.two", state="off", attributes={}),
        "light.three": State(entity_id="light.three", state="off", attributes={}),
    }

    def get_state(*, entity_id: str) -> State:
        return states[entity_id]

    client.get_state.side_effect = get_state
    controller = HassLightController(
        client,
        0,
        entity_ids=["light.measure"],
        wait=lambda _seconds: None,
        state_confirm_timeout=0,
    )
    controller.change_light_state(LutMode.BRIGHTNESS, on=True, bri=100)
    client.trigger_service.assert_called_once_with(
        "light",
        "turn_on",
        entity_id=["light.one", "light.two", "light.three"],
        brightness=100,
        transition=0,
    )


def test_confirms_members_are_on_before_returning() -> None:
    client = _mock_client()
    polls = {"light.slow": 0}
    states = {
        "light.measure": State(
            entity_id="light.measure",
            state="on",
            attributes={"entity_id": ["light.fast", "light.slow"]},
        ),
        "light.fast": State(entity_id="light.fast", state="on", attributes={"brightness": 80}),
        "light.slow": State(entity_id="light.slow", state="off", attributes={}),
    }

    def get_state(*, entity_id: str) -> State:
        if entity_id == "light.slow":
            polls["light.slow"] += 1
            if polls["light.slow"] >= 3:
                return State(entity_id="light.slow", state="on", attributes={"brightness": 80})
        return states[entity_id]

    client.get_state.side_effect = get_state
    waited: list[float] = []
    controller = HassLightController(
        client,
        0,
        entity_ids=["light.measure"],
        wait=waited.append,
        state_confirm_timeout=5,
    )
    controller.change_light_state(LutMode.COLOR_TEMP, on=True, bri=80, ct=200)
    assert polls["light.slow"] >= 3
    assert waited


def test_confirms_a_single_light_before_returning() -> None:
    polls = {"n": 0}

    def get_state(*, entity_id: str) -> State:
        polls["n"] += 1
        if polls["n"] < 3:
            return State(entity_id=entity_id, state="off", attributes={})
        return State(entity_id=entity_id, state="on", attributes={"brightness": 100})

    client = _mock_client()
    client.get_state.side_effect = get_state
    waited: list[float] = []
    controller = HassLightController(
        client,
        0,
        entity_ids=["light.only"],
        wait=waited.append,
        state_confirm_timeout=5,
    )
    controller.change_light_state(LutMode.BRIGHTNESS, on=True, bri=100)
    assert polls["n"] >= 3
    assert waited


def test_confirm_does_not_advertise_the_timeout_as_a_scheduled_wait() -> None:
    polls = {"n": 0}

    def get_state(*, entity_id: str) -> State:
        polls["n"] += 1
        if polls["n"] < 2:
            return State(entity_id=entity_id, state="off", attributes={})
        return State(entity_id=entity_id, state="on", attributes={"brightness": 100})

    client = _mock_client()
    client.get_state.side_effect = get_state
    controller = HassLightController(
        client,
        0,
        entity_ids=["light.only"],
        wait=lambda _seconds: None,
        state_confirm_timeout=5,
    )
    controller.change_light_state(LutMode.BRIGHTNESS, on=True, bri=100)
    assert polls["n"] >= 2


def test_confirm_timeout_is_a_hard_error() -> None:
    client = _mock_client()
    states = {
        "light.measure": State(
            entity_id="light.measure",
            state="on",
            attributes={"entity_id": ["light.fast", "light.slow"]},
        ),
        "light.fast": State(entity_id="light.fast", state="on", attributes={"brightness": 80}),
        "light.slow": State(entity_id="light.slow", state="off", attributes={}),
    }
    client.get_state.side_effect = lambda *, entity_id: states[entity_id]
    controller = HassLightController(
        client,
        0,
        entity_ids=["light.measure"],
        wait=lambda _seconds: None,
        state_confirm_timeout=0.01,
    )
    with pytest.raises(ControllerError, match="light.slow") as error:
        controller.change_light_state(LutMode.BRIGHTNESS, on=True, bri=80)
    assert "poison" in str(error.value)
    assert "off, brightness=None" in str(error.value)


def test_confirm_waits_until_reported_brightness_matches_the_command() -> None:
    polls = {"n": 0}

    def get_state(*, entity_id: str) -> State:
        polls["n"] += 1
        # Home Assistant often reports on at the previous brightness (or while the
        # LED is still off) before the commanded level arrives.
        brightness = 1 if polls["n"] < 3 else 255
        return State(entity_id=entity_id, state="on", attributes={"brightness": brightness})

    client = _mock_client()
    client.get_state.side_effect = get_state
    waited: list[float] = []
    controller = HassLightController(
        client,
        0,
        entity_ids=["light.only"],
        wait=waited.append,
        state_confirm_timeout=5,
    )
    controller.change_light_state(LutMode.BRIGHTNESS, on=True, bri=255)
    assert polls["n"] >= 3
    assert waited


def test_confirm_accepts_brightness_within_two_steps() -> None:
    client = _mock_client()
    client.get_state.side_effect = lambda *, entity_id: State(
        entity_id=entity_id,
        state="on",
        attributes={"brightness": 254},
    )
    controller = HassLightController(
        client,
        0,
        entity_ids=["light.only"],
        wait=lambda _seconds: None,
        state_confirm_timeout=5,
    )
    controller.change_light_state(LutMode.BRIGHTNESS, on=True, bri=255)


def test_confirm_timeout_is_a_hard_error_when_brightness_never_matches() -> None:
    client = _mock_client()
    client.get_state.side_effect = lambda *, entity_id: State(
        entity_id=entity_id,
        state="on",
        attributes={"brightness": 1},
    )
    controller = HassLightController(
        client,
        0,
        entity_ids=["light.only"],
        wait=lambda _seconds: None,
        state_confirm_timeout=0.01,
    )
    with pytest.raises(ControllerError, match="light.only") as error:
        controller.change_light_state(LutMode.BRIGHTNESS, on=True, bri=255)
    assert "poison" in str(error.value)
    assert "want 255" in str(error.value)


def test_turn_off() -> None:
    client = _mock_client()
    _get_instance(client).change_light_state(LutMode.BRIGHTNESS, on=False)
    client.trigger_service.assert_called_once_with(
        "light",
        "turn_off",
        entity_id="light.test",
        isolated=True,
    )


@pytest.mark.parametrize("connection_error", [HomeassistantAPIError("Error"), BrokenPipeError(32, "Broken pipe")])
def test_change_light_state_error(connection_error: Exception) -> None:
    client = _mock_client()
    client.trigger_service.side_effect = connection_error
    controller = _get_instance(client)
    with pytest.raises(ApiConnectionError) as error:
        controller.change_light_state(LutMode.BRIGHTNESS, on=True, bri=100)

    assert error.value.__cause__ is connection_error


def test_connection_validation() -> None:
    client = _mock_client()
    client.get_config.side_effect = HomeassistantAPIError("Error")
    with pytest.raises(ApiConnectionError):
        HassLightController(client, 0, entity_ids=["light.test"])


def test_controller_requires_an_entity() -> None:
    with pytest.raises(ValueError, match="at least one entity"):
        HassLightController(_mock_client(), 0, entity_ids=[])


def test_multiple_entities_are_targeted_together_with_their_common_capabilities() -> None:
    client = _mock_client()
    states = {
        "light.one": State(
            entity_id="light.one",
            state="on",
            attributes={
                "min_color_temp_kelvin": 2000,
                "max_color_temp_kelvin": 5000,
                "effect_list": ["one", "shared"],
                "brightness": 100,
            },
        ),
        "light.two": State(
            entity_id="light.two",
            state="on",
            attributes={
                "min_color_temp_kelvin": 2500,
                "max_color_temp_kelvin": 6500,
                "effect_list": ["shared", "two"],
                "brightness": 100,
            },
        ),
    }
    client.get_state.side_effect = lambda *, entity_id: states[entity_id]
    controller = HassLightController(client, 0, entity_ids=["light.one", "light.two"], state_confirm_timeout=0)

    controller.change_light_state(LutMode.BRIGHTNESS, bri=100)
    client.trigger_service.assert_called_once_with(
        "light",
        "turn_on",
        entity_id=["light.one", "light.two"],
        brightness=100,
        transition=0,
    )
    info = controller.get_light_info()
    assert (info.min_mired, info.max_mired) == (200, 400)
    assert controller.get_effect_list() == ["shared"]


def _get_instance(client: MagicMock | None = None) -> HassLightController:
    return HassLightController(
        client or _mock_client(),
        0,
        entity_ids=["light.test"],
        state_confirm_timeout=0,
    )


def _mock_client() -> MagicMock:
    client = MagicMock(spec=HomeAssistantManager)
    client.get_config.return_value = {}
    return client
