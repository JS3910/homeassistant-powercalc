import json
import logging
from pathlib import Path

from measure.controller.light.const import LutMode
from measure.controller.light.spec import DummyLightControllerSpec
from measure.dummy_load import DummyLoadCalibration
from measure.ha_app.contribution import (
    ContributionApiCoordinator,
    ContributionPreviewResponse,
    ContributionState,
    ContributionStatus,
)
from measure.ha_app.session import SessionEvent, SessionEventType, SessionSnapshot, SessionState, utc_now
from measure.ha_app.shelly_credentials import ShellyCredentials
from measure.ha_app.storage import SessionStorage
from measure.powermeter.spec import DummyPowerMeterSpec
from measure.request import (
    LightMeasurementRequest,
    RecorderMeasurementRequest,
    RecorderProfileRecipe,
    RecorderPurpose,
    ResumePolicy,
)
from measure.tuning import MeasurementParameters
import pytest


def light_request() -> LightMeasurementRequest:
    return LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=DummyPowerMeterSpec(),
        controller=DummyLightControllerSpec(),
    )


def snapshot(state: SessionState = SessionState.READY) -> SessionSnapshot:
    now = utc_now()
    return SessionSnapshot(id="a1b2-c3d4", state=state, created_at=now, updated_at=now)


def test_storage_round_trips_current_session(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(), light_request())

    loaded = storage.load_current()

    assert loaded is not None
    assert loaded.id == "a1b2-c3d4"
    assert storage.load_request(loaded.id).model_id == "LCT010"
    directory = storage.session_directory(loaded.id)
    persisted_request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    persisted_state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    assert persisted_request["measure_type"] == "light"
    assert persisted_state["state"] == loaded.state


def test_storage_round_trips_run_started_at(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(
        SessionSnapshot(
            id="a1b2-c3d4",
            state=SessionState.RUNNING,
            created_at="2026-09-07T20:00:00Z",
            updated_at="2026-09-07T20:01:00Z",
            completed=3,
            total=10,
            run_started_at="2026-09-07T20:00:10Z",
        ),
        light_request(),
    )

    loaded = storage.load_snapshot("a1b2-c3d4")

    assert loaded.run_started_at == "2026-09-07T20:00:10Z"
    assert loaded.completed == 3
    assert loaded.total == 10


def test_storage_round_trips_activity_reason(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(
        SessionSnapshot(
            id="a1b2-c3d4",
            state=SessionState.RUNNING,
            created_at="2026-09-07T20:00:00Z",
            updated_at="2026-09-07T20:01:00Z",
            phase="Discovering envelope",
            activity_reason="Hardcoded 100% HS outline.",
        ),
        light_request(),
    )

    loaded = storage.load_snapshot("a1b2-c3d4")

    assert loaded.activity_reason == "Hardcoded 100% HS outline."


def test_unknown_model_uses_a_session_scoped_artifact_directory(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    first = storage.artifact_directory("first", "")
    second = storage.artifact_directory("second", "")
    assert first == storage.output_directory("first") / "measurement"
    assert first != second
    assert storage.artifact_directory("first", "LCT010") == storage.output_directory("first") / "LCT010"


def test_storage_lists_retained_sessions_and_clears_deleted_current_pointer(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    first = SessionSnapshot(
        id="first-session",
        state=SessionState.COMPLETED,
        created_at="2026-07-12T12:00:00Z",
        updated_at="2026-07-12T12:05:00Z",
    )
    second = SessionSnapshot(
        id="second-session",
        state=SessionState.CANCELLED,
        created_at="2026-07-13T12:00:00Z",
        updated_at="2026-07-13T12:05:00Z",
    )
    storage.create(first, light_request())
    storage.create(second, light_request())
    (storage.sessions_root / "incomplete-session").mkdir()

    retained = storage.list_sessions()

    assert [item.id for item in retained] == [second.id, first.id]
    assert storage.session_size(first.id) > 0

    storage.delete_session(second.id)

    assert storage.load_current() is None
    assert storage.load_snapshot(first.id) == first


def test_storage_round_trips_bounded_event_replay(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    current = snapshot()
    storage.create(current, light_request())
    for sequence in range(1, 4):
        storage.append_event(
            current.id,
            SessionEvent(
                sequence=sequence,
                type=SessionEventType.LOG,
                created_at="2026-07-12T12:00:00Z",
                data={"message": str(sequence)},
            ),
            durable=False,
        )

    events = storage.load_events(current.id, limit=2)
    all_events = storage.load_events(current.id, limit=None)

    assert [event.sequence for event in events] == [2, 3]
    assert [event.sequence for event in all_events] == [1, 2, 3]


def test_storage_recovers_from_truncated_final_event(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    storage = SessionStorage(tmp_path)
    current = snapshot()
    storage.create(current, light_request())
    storage.append_event(
        current.id,
        SessionEvent(
            sequence=1,
            type=SessionEventType.LOG,
            created_at="2026-07-12T12:00:00Z",
            data={"message": "complete"},
        ),
    )
    event_path = storage.session_directory(current.id) / "events.jsonl"
    with event_path.open("a", encoding="utf-8") as file:
        file.write('{"sequence":2')

    with caplog.at_level(logging.WARNING, logger="measure"):
        events = storage.load_events(current.id)

    assert [event.sequence for event in events] == [1]
    assert "truncated final session event" in caplog.text


def test_running_session_becomes_resumable_after_restart(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(SessionState.RUNNING), light_request())
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir()
    (output / "brightness.csv").write_text("bri,watt\n2,1.0\n", encoding="utf-8")

    loaded = SessionStorage(tmp_path).load_current()

    assert loaded is not None
    assert loaded.state == SessionState.RESUMABLE


@pytest.mark.parametrize(
    "state",
    [
        SessionState.VALIDATING,
        SessionState.READY,
        SessionState.AWAITING_CONFIRMATION,
        SessionState.RUNNING,
        SessionState.CANCELLING,
    ],
)
def test_every_orphaned_nonterminal_session_is_recovered(tmp_path: Path, state: SessionState) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(state), light_request())

    loaded = SessionStorage(tmp_path).load_current()

    assert loaded is not None
    assert loaded.state == SessionState.FAILED
    assert "app restarted" in str(loaded.error).lower()


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "bri,watt\n",
        "wrong,watt\n1,1.0\n",
        "bri,watt\n1,",
        "bri,watt\n999,1.0\n",
    ],
)
def test_interrupted_session_without_compatible_complete_row_fails(tmp_path: Path, contents: str) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(SessionState.RUNNING), light_request())
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir()
    (output / "brightness.csv").write_text(contents, encoding="utf-8")

    loaded = SessionStorage(tmp_path).load_current()

    assert loaded is not None
    assert loaded.state == SessionState.FAILED
    assert not SessionStorage(tmp_path).can_resume(loaded.id)


def test_storage_recognizes_only_existing_complex_profile_recordings_as_analysable(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    request = RecorderMeasurementRequest(
        recorder_purpose=RecorderPurpose.COMPLEX_PROFILE,
        profile_recipe=RecorderProfileRecipe.GENERIC,
        tracked_entity_ids=("switch.device",),
        power_meter=DummyPowerMeterSpec(),
    )
    storage.create(snapshot(SessionState.COMPLETED), request)

    assert not storage.can_analyse("missing-session")
    assert not storage.can_analyse("a1b2-c3d4")

    output = storage.artifact_directory("a1b2-c3d4", request.model_id)
    output.mkdir()
    (output / "record.jsonl").write_text("recording", encoding="utf-8")

    assert storage.can_analyse("a1b2-c3d4")


def test_file_path_rejects_traversal(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(), light_request())

    with pytest.raises(ValueError, match="Path escapes session output directory"):
        storage.file_path("a1b2-c3d4", "../../secret")


def test_effect_output_is_recognized_as_resumable(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    request = light_request().model_copy(update={"modes": {LutMode.EFFECT}})
    storage.create(snapshot(SessionState.RUNNING), request)
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir()
    (output / "effect.csv").write_text("effect,bri,watt\nnightlight,205,3.0\n", encoding="utf-8")

    loaded = SessionStorage(tmp_path).load_current()

    assert loaded is not None
    assert loaded.state == SessionState.RESUMABLE


def test_effect_output_off_the_measurement_grid_is_not_resumable(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    request = light_request().model_copy(update={"modes": {LutMode.EFFECT}})
    storage.create(snapshot(SessionState.RUNNING), request)
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir()
    # bri=200 is not produced by the effect brightness grid, so the runner could not resume it.
    (output / "effect.csv").write_text("effect,bri,watt\nnightlight,200,3.0\n", encoding="utf-8")

    loaded = SessionStorage(tmp_path).load_current()

    assert loaded is not None
    assert loaded.state == SessionState.FAILED


def test_smart_invented_color_temp_row_is_resumable(tmp_path: Path) -> None:
    """A rail point is not on the initial 100% sweep; resume still replays from it."""

    storage = SessionStorage(tmp_path)
    request = light_request().model_copy(
        update={
            "modes": {LutMode.COLOR_TEMP},
            "parameters": MeasurementParameters(smart_sampling=True),
        },
    )
    storage.create(snapshot(SessionState.RUNNING), request)
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir()
    (output / "color_temp.csv").write_text("bri,mired,watt\n164,555,2.86\n", encoding="utf-8")

    loaded = SessionStorage(tmp_path).load_current()

    assert loaded is not None
    assert loaded.state == SessionState.RESUMABLE


def test_color_temp_output_beyond_the_generic_mired_placeholder_is_still_resumable(tmp_path: Path) -> None:
    """A real light's mired range is unknown offline, so a generic placeholder range is used
    to rebuild its plan (see `_variation_matches_request`). A warm-white bulb reaching down to
    1808K (553 mired) legitimately exceeds that placeholder's 500-mired ceiling; the fix removed
    the bound on `ct` entirely rather than tightening it, since the placeholder is not a real
    device range and any fixed bound risks rejecting some real light's legitimate value the
    same way. Only `bri` (validated against the plan) needs to hold.
    """
    storage = SessionStorage(tmp_path)
    request = light_request().model_copy(update={"modes": {LutMode.COLOR_TEMP}})
    storage.create(snapshot(SessionState.RUNNING), request)
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir()
    (output / "color_temp.csv").write_text("bri,mired,watt\n1,553,0.30\n", encoding="utf-8")

    loaded = SessionStorage(tmp_path).load_current()

    assert loaded is not None
    assert loaded.state == SessionState.RESUMABLE


def test_interrupted_extend_session_is_resumable_with_off_grid_seed_row(tmp_path: Path) -> None:
    """A refine copies the seed LUT; the last seed row is usually not on the new grid."""

    storage = SessionStorage(tmp_path)
    request = light_request().model_copy(
        update={
            "modes": {LutMode.COLOR_TEMP},
            "resume_policy": ResumePolicy.EXTEND,
            "seed_session_id": "seed-session",
            "parameters": MeasurementParameters(
                min_kelvin=1801,
                max_kelvin=2100,
                ct_mired_divisions=2,
                ct_bri_all=True,
                max_brightness=199,
            ),
        },
    )
    storage.create(snapshot(SessionState.RUNNING), request)
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir()
    (output / "color_temp.csv").write_text("bri,mired,watt\n98,465,0.52\n", encoding="utf-8")

    loaded = SessionStorage(tmp_path).load_current()

    assert loaded is not None
    assert loaded.state == SessionState.RESUMABLE
    assert SessionStorage(tmp_path).can_resume(loaded.id)


def test_settings_recover_from_corrupt_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    storage = SessionStorage(tmp_path)
    (tmp_path / "settings.json").write_text("not json")

    with caplog.at_level(logging.WARNING, logger="measure"):
        settings = storage.load_settings()

    assert settings.default_power_entity_id is None
    assert "using defaults" in caplog.text


def test_storage_persists_shelly_credentials_privately(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)

    storage.save_shelly_credentials(ShellyCredentials(password="device-password"))  # noqa: S106

    credential_path = tmp_path / "shelly_credentials.json"
    assert storage.load_shelly_credentials() == ShellyCredentials(password="device-password")  # noqa: S106
    assert credential_path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "settings.json").exists()

    storage.clear_shelly_credentials()
    assert storage.load_shelly_credentials() is None
    assert not credential_path.exists()


def test_storage_round_trips_global_and_session_dummy_load_calibration(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(), light_request())
    calibration = DummyLoadCalibration(
        description="40 W incandescent bulb",
        resistance=1322.5,
        calibrated_at="2026-07-16T12:00:00Z",
        power_meter_fingerprint="meter-fingerprint",
    )

    storage.save_dummy_load_calibration(calibration)
    storage.save_session_dummy_load_calibration("a1b2-c3d4", calibration)

    assert storage.load_dummy_load_calibration() == calibration
    assert storage.load_session_dummy_load_calibration("a1b2-c3d4") == calibration


def test_contribution_state_recovers_interrupted_submission(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    preview = ContributionPreviewResponse(
        session_id="a1b2-c3d4",
        title="Add profile",
        body="Body",
        eligible=True,
        manufacturer_name="Signify",
        manufacturer_directory="signify",
        model_id="LCT010",
        product_name="Hue lamp",
        contributor="measure-user",
        files=[],
        commit_message="feat(profile): add signify LCT010",
        pr_title="Add signify LCT010 power profile",
        pr_body="Body",
        branch_name="powercalc-profile-signify-lct010",
    )
    storage.save_contribution_status(
        ContributionStatus(
            state=ContributionState.SUBMITTING,
            session_id="a1b2-c3d4",
            preview=preview,
            updated_at="2026-07-12T12:00:00Z",
        ),
    )

    status = ContributionApiCoordinator(storage).status()

    assert status.state == ContributionState.FAILED
    assert status.session_id == "a1b2-c3d4"
    assert status.error == "App stopped during contribution submission; preview can be submitted again"


def test_storage_ignores_invalid_dummy_load_calibration(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    storage = SessionStorage(tmp_path)
    (tmp_path / "dummy_load_calibration.json").write_text('{"resistance": -1}', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="measure"):
        calibration = storage.load_dummy_load_calibration()

    assert calibration is None
    assert "invalid dummy-load calibration" in caplog.text


def test_create_can_skip_marking_the_session_current(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(SessionState.COMPLETED), light_request(), set_current=False)

    assert storage.load_current() is None
    assert storage.load_snapshot("a1b2-c3d4").state == SessionState.COMPLETED


def test_has_complete_lut_row_accepts_an_off_grid_row(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(SessionState.COMPLETED), light_request(), set_current=False)
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir()
    (output / "brightness.csv").write_text("bri,watt\n999,1.0\n", encoding="utf-8")

    assert storage.has_complete_lut_row("a1b2-c3d4") is True
    assert storage.has_lut_csv("a1b2-c3d4") is True
    assert storage.can_resume("a1b2-c3d4") is False


def test_has_lut_csv_is_false_without_a_measurement_file(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(SessionState.COMPLETED), light_request(), set_current=False)
    assert storage.has_lut_csv("a1b2-c3d4") is False


def test_session_listing_stats_count_output_files_and_all_bytes(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(SessionState.COMPLETED), light_request(), set_current=False)
    output = storage.artifact_directory("a1b2-c3d4", "LCT010")
    output.mkdir(parents=True)
    (output / "hs.csv").write_text("bri,hue,sat,watt\n255,1,255,1.0\n", encoding="utf-8")
    (storage.session_directory("a1b2-c3d4") / "events.jsonl").write_text("{}\n", encoding="utf-8")

    file_count, size = storage.session_listing_stats("a1b2-c3d4")
    assert file_count == 1
    assert size == storage.session_size("a1b2-c3d4")
    assert size > 0


def test_list_sessions_loads_legacy_native_increment_fields(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = SessionStorage(tmp_path)
    storage.create(snapshot(SessionState.COMPLETED), light_request(), set_current=False)
    path = storage.session_directory("a1b2-c3d4") / "request.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["parameters"] = {
        **payload.get("parameters", {}),
        "ct_mired_steps": 10,
        "hs_hue_steps": 2731,
        "hs_sat_steps": 32,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="measure"):
        sessions = SessionStorage(tmp_path).list_sessions()

    assert [item.id for item in sessions] == ["a1b2-c3d4"]
    assert "incompatible measurement session" not in caplog.text


def test_copy_lut_artifacts_copies_csv_and_raw_into_a_new_model_directory(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    seed = snapshot(SessionState.COMPLETED)
    dest = SessionSnapshot(
        id="dest-session",
        state=SessionState.READY,
        created_at=seed.created_at,
        updated_at=seed.updated_at,
    )
    storage.create(seed, light_request(), set_current=False)
    storage.create(dest, light_request().model_copy(update={"model_id": "OTHER"}), set_current=False)
    source = storage.artifact_directory(seed.id, "LCT010")
    source.mkdir()
    (source / "brightness.csv").write_text("bri,watt\n1,1.0\n", encoding="utf-8")
    (source / "brightness.raw.jsonl").write_text("{}\n", encoding="utf-8")
    (source / "on_off_bounds.json").write_text(
        '{"standby_w_per_lamp": 0.05, "minimum_on_w_per_lamp": 0.26}\n',
        encoding="utf-8",
    )

    storage.copy_lut_artifacts(seed.id, dest.id, "OTHER")

    copied = storage.artifact_directory(dest.id, "OTHER")
    assert (copied / "brightness.csv").read_text(encoding="utf-8") == "bri,watt\n1,1.0\n"
    assert (copied / "brightness.raw.jsonl").is_file()
    assert (copied / "on_off_bounds.json").is_file()
