from measure.clock import parse_utc
from measure.ha_app.session import SessionSnapshot, SessionState
from measure.ha_app.session_display import (
    brand_model_title,
    looks_like_entity_list,
    points_this_run,
    raw_since_timestamp,
    session_duration_seconds,
    session_family_key,
    session_identity,
    session_modes,
)
from measure.home_assistant_entities import EntityDescriptor
from measure.request import LightMeasurementRequest


def _request(**updates: object) -> LightMeasurementRequest:
    payload: dict[str, object] = {
        "measure_type": "light",
        "model_id": "36871",
        "product_name": "",
        "session_name": "Entrance Spot Solo, Entrance Spot Trio 1",
        "measure_device": "Zhurui PR10",
        "controller": {"type": "dummy"},
        "power_meter": {"type": "dummy"},
        "modes": ["hs", "color_temp"],
    }
    payload.update(updates)
    return LightMeasurementRequest.model_validate(payload)


def _descriptor() -> EntityDescriptor:
    return EntityDescriptor(
        entity_id="light.entrance_spot_solo",
        name="Entrance Spot Solo",
        domain="light",
        state="on",
        attribute_names=[],
        manufacturer="IKEA of Sweden",
        model_id="36871",
        product_name="KAJPLATS GU10 CWS 470lm",
    )


def test_joined_light_names_look_like_an_entity_list() -> None:
    assert looks_like_entity_list("Entrance Spot Solo, Entrance Spot Trio 1")
    assert looks_like_entity_list("light.entrance_spot_solo")
    assert not looks_like_entity_list("Desk lamp")


def test_brand_and_model_title_does_not_repeat_the_manufacturer() -> None:
    assert brand_model_title("IKEA of Sweden", "KAJPLATS GU10 CWS 470lm") == (
        "IKEA of Sweden · KAJPLATS GU10 CWS 470lm"
    )
    assert brand_model_title("IKEA of Sweden", "IKEA of Sweden KAJPLATS") == "IKEA of Sweden KAJPLATS"


def test_session_identity_prefers_live_device_registry_over_joined_names() -> None:
    manufacturer, product, title = session_identity(_request(), _descriptor())
    assert manufacturer == "IKEA of Sweden"
    assert product == "KAJPLATS GU10 CWS 470lm"
    assert title == "IKEA of Sweden · KAJPLATS GU10 CWS 470lm"


def test_session_identity_falls_back_to_a_user_session_name() -> None:
    request = _request(model_id="", product_name="", session_name="Desk lamp")
    _, _, title = session_identity(request)
    assert title == "Desk lamp"


def test_session_identity_ignores_a_joined_session_name_when_the_product_is_known() -> None:
    request = _request(product_name="KAJPLATS GU10 CWS 470lm")
    _, _, title = session_identity(request)
    assert title == "KAJPLATS GU10 CWS 470lm"


def test_family_key_groups_the_same_model_on_the_same_meter() -> None:
    request = _request()
    assert session_family_key(request, product="KAJPLATS GU10 CWS 470lm") == "light|36871|zhurui pr10"
    other = _request(measure_device="Shelly Plug")
    assert session_family_key(other) != session_family_key(request)


def test_session_modes_are_sorted() -> None:
    assert session_modes(_request()) == ["color_temp", "hs"]


def test_session_duration_uses_the_run_clock() -> None:
    snapshot = SessionSnapshot(
        id="s1",
        state=SessionState.COMPLETED,
        created_at="2026-09-13T08:00:00Z",
        updated_at="2026-09-13T08:12:00Z",
        run_started_at="2026-09-13T08:01:00Z",
        completed=10,
        total=10,
    )
    assert session_duration_seconds(snapshot) == 660


def test_points_this_run_prefers_the_raw_log() -> None:
    assert points_this_run(completed=17833, already_measured=0, recorded=214) == 214
    assert points_this_run(completed=18044, already_measured=17833, recorded=None) == 211
    assert points_this_run(completed=40, already_measured=0, recorded=None) == 40


def test_raw_since_uses_created_at_when_the_run_never_started() -> None:
    snapshot = SessionSnapshot(
        id="s1",
        state=SessionState.CANCELLED,
        created_at="2026-09-11T19:17:09Z",
        updated_at="2026-09-11T19:17:26Z",
    )
    assert raw_since_timestamp(snapshot) == parse_utc(snapshot.created_at).timestamp()


def test_raw_since_prefers_the_run_clock() -> None:
    snapshot = SessionSnapshot(
        id="s1",
        state=SessionState.COMPLETED,
        created_at="2026-09-11T19:17:09Z",
        updated_at="2026-09-11T19:32:00Z",
        run_started_at="2026-09-11T19:18:00Z",
    )
    assert raw_since_timestamp(snapshot) == parse_utc("2026-09-11T19:18:00Z").timestamp()
