from pathlib import Path
from threading import Event, Thread
import time
from unittest.mock import MagicMock, patch

from measure.controller.light.spec import DummyLightControllerSpec
from measure.execution import LightOperatingPoint
from measure.ha_app.coordinator import (
    MeasurementCoordinator,
    MergeEligibilityError,
    SessionConflictError,
    SessionExecutionContext,
    SessionMeasurementService,
)
from measure.ha_app.interaction import SessionInteraction
from measure.ha_app.session import SessionControl, SessionEventType, SessionSnapshot, SessionState
from measure.ha_app.storage import SessionStorage
from measure.powermeter.powermeter import PowerMeasurementResult, PowerMeter
from measure.powermeter.spec import DummyPowerMeterSpec
from measure.request import (
    AverageMeasurementRequest,
    LightMeasurementRequest,
    MeasurementRequest,
    RecorderMeasurementRequest,
    RecorderProfileRecipe,
    RecorderPurpose,
    ResumePolicy,
)
from measure.runner.average import AverageRunner
from measure.runner.recorder import RecorderRunner
from measure.runner.runner import RunnerResult
from measure.tuning import MeasurementParameters
from measure.util.measure_util import MeasurementResult, MeasureUtil
import pytest


def light_request() -> LightMeasurementRequest:
    return LightMeasurementRequest(
        model_id="LCT010",
        product_name="Test light",
        measure_device="Test meter",
        power_meter=DummyPowerMeterSpec(),
        controller=DummyLightControllerSpec(),
    )


def wait_for_state(coordinator: MeasurementCoordinator, state: SessionState) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if coordinator.current and coordinator.current.state == state:
            return
        time.sleep(0.01)
    raise AssertionError(f"Session did not reach {state}")


class CompletingService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        directory = context.artifact_directory
        directory.mkdir(parents=True)
        (directory / "brightness.csv").write_text("bri,watt\n1,1.0\n", encoding="utf-8")
        control.progress(completed=1, total=1, mode="brightness", estimated_remaining="0s")
        return RunnerResult(model_json_data={})


class BlockingService(SessionMeasurementService):
    def __init__(self, started: Event) -> None:
        self.started = started

    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        self.started.set()
        control.wait(60)
        raise AssertionError("Cancelled wait returned")


class RecorderService(SessionMeasurementService):
    def __init__(self, sample_recorded: Event) -> None:
        self.sample_recorded = sample_recorded

    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        assert isinstance(request, RecorderMeasurementRequest)
        context.artifact_directory.mkdir(parents=True)
        measure_util = MagicMock(spec=MeasureUtil)

        def take_measurement(_: float) -> MeasurementResult:
            self.sample_recorded.set()
            return MeasurementResult(power=4.2, voltages=[])

        measure_util.take_measurement.side_effect = take_measurement
        return RecorderRunner(measure_util, SessionInteraction(control)).run(
            request,
            str(context.artifact_directory),
        )


class SamplingService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        control.sample(4.2)
        return RunnerResult(model_json_data={})


class WarningService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        control.log("Repeated warning", warning=True)
        return RunnerResult(model_json_data={})


class EntityStateService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        control.entity_states({"vacuum.robot": "cleaning", "sensor.robot_battery": "42"})
        return RunnerResult(model_json_data={})


class OperatingPointService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        control.operating_point(LightOperatingPoint(type="light", on=True, brightness=128))
        control.wait(60)
        raise AssertionError("Cancelled wait returned")


class CheckpointService(SessionMeasurementService):
    def __init__(self, continued: Event) -> None:
        self.continued = continued

    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        control.phase("Preparing operator checkpoint")
        control.confirm(
            "Place the device on its charger, then start the measurement.",
            action="Start charging measurement",
        )
        self.continued.set()
        return RunnerResult(model_json_data={})


class ReasonThenProgressService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        del request, context
        control.phase(
            "Discovering envelope",
            reason="Hardcoded 100% HS outline: hue ring, white, and primary saturations.",
        )
        control.progress(completed=1, total=10, mode="hs", estimated_remaining="9s")
        control.phase("Full-brightness warm-up (1 of 2): sending command")
        return RunnerResult(model_json_data={})


class RepeatedWarningService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        del request, context
        control.log("Repeated warning", warning=True)
        control.log("Repeated warning", warning=True)
        control.log("Repeated warning", warning=True)
        return RunnerResult(model_json_data={})


class WarningThenProgressService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        del request, context
        control.log("Failed to change light state: boom. Retrying...", warning=True)
        control.progress(completed=1, total=2, mode="color_temp", estimated_remaining="10s")
        return RunnerResult(model_json_data={})


def test_progress_and_later_phases_keep_activity_reason(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), ReasonThenProgressService)

    coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.current is not None
    assert coordinator.current.activity_reason == (
        "Hardcoded 100% HS outline: hue ring, white, and primary saturations."
    )


def test_coordinator_deduplicates_repeated_warnings(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), RepeatedWarningService)

    session = coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.current is not None
    assert coordinator.current.warnings == ("Repeated warning",)
    warning_events = [
        event for event in coordinator.events_since(0, session.id) if event.type == SessionEventType.WARNING
    ]
    assert [event.data["message"] for event in warning_events] == [
        "Repeated warning",
        "Repeated warning",
        "Repeated warning",
    ]


def test_coordinator_collapses_persisted_warning_duplicates_when_resuming(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    snapshot = SessionSnapshot(
        id="resumable-session",
        state=SessionState.CANCELLED,
        created_at="2026-07-12T12:00:00Z",
        updated_at="2026-07-12T12:05:00Z",
        warnings=("Repeated warning", "Repeated warning", "Repeated warning"),
    )
    storage.create(snapshot, light_request())
    output = storage.artifact_directory(snapshot.id, "LCT010")
    output.mkdir()
    (output / "brightness.csv").write_text("bri,watt\n1,1.0\n", encoding="utf-8")
    coordinator = MeasurementCoordinator(storage, RepeatedWarningService)

    coordinator.resume(snapshot.id)
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.get(snapshot.id).warnings == ("Repeated warning",)


def test_a_warning_banner_clears_once_the_run_continues(tmp_path: Path) -> None:
    """A recovered-from warning must not stay up as a persistent banner. Progress
    after the warning is the signal that the run continued without needing input.
    """
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), WarningThenProgressService)

    coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.current is not None
    assert coordinator.current.warnings == ()


def test_coordinator_completes_and_persists_files(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), CompletingService)

    session = coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.current is not None
    assert coordinator.current.progress == 100
    assert coordinator.current.run_started_at is not None
    assert coordinator.current.files == ("LCT010/brightness.csv",)
    assert [(event.sequence, event.data["state"]) for event in coordinator.events_since(1, session.id)] == [
        (2, SessionState.COMPLETED),
    ]


def test_coordinator_projects_latest_recorder_entity_states(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), EntityStateService)

    coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.current is not None
    assert coordinator.current.entity_states == {
        "vacuum.robot": "cleaning",
        "sensor.robot_battery": "42",
    }


def test_coordinator_notifies_session_state_listeners(tmp_path: Path) -> None:
    started = Event()
    terminal_notification = Event()
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), lambda: BlockingService(started))
    notifications: list[SessionState | None] = []

    def record_notification() -> None:
        state = coordinator.current.state if coordinator.current is not None else None
        notifications.append(state)
        if state == SessionState.CANCELLED:
            terminal_notification.set()

    unsubscribe = coordinator.subscribe(record_notification)

    session = coordinator.start(light_request())
    assert started.wait(1)
    coordinator.cancel(session.id)
    wait_for_state(coordinator, SessionState.CANCELLED)
    assert terminal_notification.wait(1)
    unsubscribe()
    coordinator.delete(session.id)

    assert notifications == [SessionState.RUNNING, SessionState.CANCELLING, SessionState.CANCELLED]


def test_request_shutdown_stop_cooperatively_cancels_and_waits_for_the_worker(tmp_path: Path) -> None:
    """The app's own shutdown hook calls this before the process actually exits, so a
    container stop (add-on update, Supervisor cold backup, ...) lands the worker on the
    CANCELLED checkpoint -- always resumable -- rather than abandoning it mid-write.
    """
    started = Event()
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), lambda: BlockingService(started))
    session = coordinator.start(light_request())
    assert started.wait(1)

    coordinator.request_shutdown_stop(timeout=2.0)

    assert coordinator.current is not None
    assert coordinator.current.state == SessionState.CANCELLED
    coordinator.delete(session.id)


def test_request_shutdown_stop_is_a_no_op_without_an_active_session(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), lambda: CompletingService())

    coordinator.request_shutdown_stop(timeout=2.0)  # Must not raise with nothing running.

    assert coordinator.current is None


def test_stopping_recorder_marks_session_completed(tmp_path: Path) -> None:
    sample_recorded = Event()
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), lambda: RecorderService(sample_recorded))

    session = coordinator.start(RecorderMeasurementRequest(power_meter=DummyPowerMeterSpec()))
    wait_for_state(coordinator, SessionState.AWAITING_CONFIRMATION)
    coordinator.confirm(session.id)
    assert sample_recorded.wait(1)

    coordinator.cancel(session.id)
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.current is not None
    assert coordinator.current.summary is not None
    assert coordinator.current.summary["Samples recorded"] == "1"
    assert coordinator.current.files == ("measurement/record.csv",)


@pytest.mark.parametrize("stop_before_confirmation", [False, True])
def test_stopping_average_keeps_result_after_sampling(tmp_path: Path, stop_before_confirmation: bool) -> None:
    sample_recorded = Event()

    class AverageService(SessionMeasurementService):
        def run(
            self,
            request: MeasurementRequest,
            control: SessionControl,
            context: SessionExecutionContext,
        ) -> RunnerResult:
            assert isinstance(request, AverageMeasurementRequest)
            meter = MagicMock(PowerMeter)
            meter.get_power.return_value = PowerMeasurementResult(power=4.2, voltage=230.0, updated=time.time())
            util = MeasureUtil(
                meter,
                MeasurementParameters(),
                include_voltage=lambda: True,
                wait=control.wait,
                on_sample=lambda _: sample_recorded.set(),
            )
            return AverageRunner(util, SessionInteraction(control)).run(request, "")

    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), AverageService)
    session = coordinator.start(AverageMeasurementRequest(power_meter=DummyPowerMeterSpec(), duration=60))
    wait_for_state(coordinator, SessionState.AWAITING_CONFIRMATION)
    if not stop_before_confirmation:
        coordinator.confirm(session.id)
        assert sample_recorded.wait(1)
    coordinator.cancel(session.id)
    wait_for_state(coordinator, SessionState.CANCELLED if stop_before_confirmation else SessionState.COMPLETED)
    assert coordinator.current is not None
    if not stop_before_confirmation:
        assert coordinator.current.summary is not None
        assert coordinator.current.summary["Average power"] == "4.2 W"
        assert coordinator.current.summary["Average voltage"] == "230.0 V"
        assert float(coordinator.current.summary["Duration"].split()[0]) < 60


def test_coordinator_isolates_session_state_listener_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), CompletingService)

    def fail_notification() -> None:
        raise RuntimeError("Observer failed")

    coordinator.subscribe(fail_notification)

    session = coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert session.id
    assert "Measurement session state listener failed" in caplog.text


def test_coordinator_rejects_concurrent_start_and_cancels(tmp_path: Path) -> None:
    started = Event()
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), lambda: BlockingService(started))
    session = coordinator.start(light_request())
    assert started.wait(1)

    duplicate = light_request()
    with pytest.raises(SessionConflictError):
        coordinator.start(duplicate)
    with pytest.raises(SessionConflictError, match="measurement session is already active"):
        coordinator.analyse(session.id)

    coordinator.cancel(session.id)
    wait_for_state(coordinator, SessionState.CANCELLED)
    assert coordinator.cancel(session.id).state == SessionState.CANCELLED


def test_session_phase_includes_a_wait_deadline() -> None:
    events: list = []
    control = SessionControl()
    control.subscribe(events.append)

    control.phase("Stabilizing light before the first reading", wait_seconds=10)

    assert events[-1].data["message"] == "Stabilizing light before the first reading"
    assert events[-1].data["wait_seconds"] == 10
    assert events[-1].data["wait_ends_at"]


def test_coordinator_serializes_recording_analysis_with_other_session_actions(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    request = RecorderMeasurementRequest(
        recorder_purpose=RecorderPurpose.COMPLEX_PROFILE,
        profile_recipe=RecorderProfileRecipe.GENERIC,
        tracked_entity_ids=("switch.device",),
        power_meter=DummyPowerMeterSpec(),
    )
    completed = SessionSnapshot(
        id="recording",
        state=SessionState.COMPLETED,
        created_at="2026-09-04T08:00:00Z",
        updated_at="2026-09-04T08:00:00Z",
        warnings=(
            "Power meter briefly stopped reporting",
            "Profile was not created: the previous analysis rejected the recording",
            "Recording analysis: skipped one malformed line",
        ),
    )
    storage.create(completed, request)
    output = storage.artifact_directory(completed.id, request.model_id)
    output.mkdir()
    (output / "record.jsonl").write_text("recording", encoding="utf-8")
    coordinator = MeasurementCoordinator(storage, CompletingService)
    analysis_started = Event()
    finish_analysis = Event()

    def analyse_recording(*_args: object, **_kwargs: object) -> dict[str, str]:
        analysis_started.set()
        assert finish_analysis.wait(1)
        return {"Recording analysis": "Profile created"}

    with patch("measure.ha_app.coordinator.RecorderAnalysisExecution") as execution_class:
        execution_class.return_value.run.side_effect = analyse_recording
        thread = Thread(target=coordinator.analyse, args=(completed.id,))
        thread.start()
        assert analysis_started.wait(1)

        with pytest.raises(SessionConflictError, match="Recording analysis is already active"):
            coordinator.start(light_request())
        with pytest.raises(SessionConflictError, match="Recording analysis is already active"):
            coordinator.resume(completed.id)
        with pytest.raises(SessionConflictError, match="already being analysed"):
            coordinator.analyse(completed.id)
        with pytest.raises(SessionConflictError, match="while its recording is being analysed"):
            coordinator.delete(completed.id)

        finish_analysis.set()
        thread.join(1)

    assert not thread.is_alive()
    assert coordinator.get(completed.id).summary == {"Recording analysis": "Profile created"}
    assert coordinator.get(completed.id).warnings == ("Power meter briefly stopped reporting",)


def test_coordinator_rejects_analysis_for_unknown_session(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), CompletingService)

    with pytest.raises(SessionConflictError, match="requested session does not exist"):
        coordinator.analyse("missing-session")

def test_coordinator_relaunches_a_failed_session_without_output(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    current = SessionSnapshot(
        id="failed",
        state=SessionState.FAILED,
        created_at="2026-07-12T12:00:00Z",
        updated_at="2026-07-12T12:00:00Z",
    )
    storage.create(current, light_request())
    started = Event()
    coordinator = MeasurementCoordinator(storage, lambda: BlockingService(started))

    resumed = coordinator.resume(current.id)

    assert started.wait(1)
    assert resumed.id == current.id
    assert resumed.state == SessionState.RUNNING


def test_coordinator_resume_clears_prior_error_and_warnings(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    current = SessionSnapshot(
        id="failed",
        state=SessionState.FAILED,
        created_at="2026-07-12T12:00:00Z",
        updated_at="2026-07-12T12:00:00Z",
        error="The meter went silent",
        warnings=("Discarding measurement: 0 watt was read from the power meter",),
    )
    storage.create(current, light_request())
    started = Event()
    coordinator = MeasurementCoordinator(storage, lambda: BlockingService(started))

    resumed = coordinator.resume(current.id)

    assert started.wait(1)
    assert resumed.error is None
    assert resumed.warnings == ()


class CapturingService(SessionMeasurementService):
    def __init__(self, started: Event) -> None:
        self.started = started
        self.request: MeasurementRequest | None = None

    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        self.request = request
        self.started.set()
        control.wait(60)
        raise AssertionError("Cancelled wait returned")


def test_coordinator_resume_keeps_extend_policy(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    current = SessionSnapshot(
        id="refine",
        state=SessionState.FAILED,
        created_at="2026-07-12T12:00:00Z",
        updated_at="2026-07-12T12:00:00Z",
        error="The existing measurement CSV does not match the configured measurement grid",
    )
    storage.create(
        current,
        light_request().model_copy(
            update={"resume_policy": ResumePolicy.EXTEND, "seed_session_id": "seed-session"},
        ),
    )
    started = Event()
    service = CapturingService(started)
    coordinator = MeasurementCoordinator(storage, lambda: service)

    coordinator.resume(current.id)

    assert started.wait(1)
    assert service.request is not None
    assert service.request.resume_policy == ResumePolicy.EXTEND
    assert service.request.seed_session_id == "seed-session"


def test_starting_a_session_retains_the_previous_one(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), CompletingService)
    first = coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)
    old_directory = coordinator.storage.session_directory(first.id)

    second = coordinator.start(light_request())

    assert old_directory.exists()
    assert {session.id for session in coordinator.sessions()} == {first.id, second.id}


def test_coordinator_resumes_a_retained_historical_session(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    old = SessionSnapshot(
        id="old-session",
        state=SessionState.CANCELLED,
        created_at="2026-07-12T12:00:00Z",
        updated_at="2026-07-12T12:05:00Z",
    )
    storage.create(old, light_request())
    output = storage.artifact_directory(old.id, "LCT010")
    output.mkdir()
    (output / "brightness.csv").write_text("bri,watt\n2,1.0\n", encoding="utf-8")
    current = SessionSnapshot(
        id="new-session",
        state=SessionState.COMPLETED,
        created_at="2026-07-13T12:00:00Z",
        updated_at="2026-07-13T12:05:00Z",
    )
    storage.create(current, light_request())
    started = Event()
    coordinator = MeasurementCoordinator(storage, lambda: BlockingService(started))

    resumed = coordinator.resume(old.id)

    assert started.wait(1)
    assert resumed.id == old.id
    assert resumed.state == SessionState.RUNNING
    assert "old-session" in (tmp_path / "current.json").read_text(encoding="utf-8")
    coordinator.cancel(old.id)
    wait_for_state(coordinator, SessionState.CANCELLED)


def test_coordinator_deduplicates_warnings_when_resuming(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    snapshot = SessionSnapshot(
        id="resumable-session",
        state=SessionState.CANCELLED,
        created_at="2026-07-12T12:00:00Z",
        updated_at="2026-07-12T12:05:00Z",
        warnings=("Repeated warning", "Repeated warning", "Repeated warning"),
    )
    storage.create(snapshot, light_request())
    output = storage.artifact_directory(snapshot.id, "LCT010")
    output.mkdir()
    (output / "brightness.csv").write_text("bri,watt\n1,1.0\n", encoding="utf-8")
    coordinator = MeasurementCoordinator(storage, WarningService)

    coordinator.resume(snapshot.id)
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.get(snapshot.id).warnings == ("Repeated warning",)
    warning_events = [
        event for event in coordinator.events_since(0, snapshot.id) if event.type == SessionEventType.WARNING
    ]
    assert [event.data["message"] for event in warning_events] == ["Repeated warning"]


def test_coordinator_deletes_only_terminal_sessions(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), CompletingService)
    completed = coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    coordinator.delete(completed.id)

    assert coordinator.sessions() == ()
    assert coordinator.current is None


def test_transient_sample_does_not_reuse_terminal_event_sequence(tmp_path: Path) -> None:
    coordinator = MeasurementCoordinator(SessionStorage(tmp_path), SamplingService)

    session = coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    events = coordinator.events_since(0, session.id)
    assert [event.sequence for event in events] == [1, 2]
    assert len({event.sequence for event in events}) == len(events)


def test_coordinator_reloads_persisted_events_for_reconnect(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    coordinator = MeasurementCoordinator(storage, CompletingService)
    session = coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.COMPLETED)

    reloaded = MeasurementCoordinator(storage, CompletingService)

    assert [event.sequence for event in reloaded.events_since(0, session.id)] == [1, 2]


def test_coordinator_projects_and_persists_operating_point(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    coordinator = MeasurementCoordinator(storage, OperatingPointService)
    session = coordinator.start(light_request())

    deadline = time.monotonic() + 1
    while coordinator.current and coordinator.current.operating_point is None and time.monotonic() < deadline:
        time.sleep(0.01)

    assert coordinator.current is not None
    assert coordinator.current.operating_point == {"type": "light", "on": True, "brightness": 128}
    persisted = storage.load_current()
    assert persisted is not None
    assert persisted.operating_point == coordinator.current.operating_point

    coordinator.cancel(session.id)
    wait_for_state(coordinator, SessionState.CANCELLED)


def test_coordinator_projects_phase_and_confirmation_message(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    continued = Event()
    coordinator = MeasurementCoordinator(storage, lambda: CheckpointService(continued))

    session = coordinator.start(light_request())
    wait_for_state(coordinator, SessionState.AWAITING_CONFIRMATION)

    assert coordinator.current is not None
    assert coordinator.current.phase == "Waiting for confirmation"
    assert coordinator.current.confirmation_message == "Place the device on its charger, then start the measurement."
    assert coordinator.current.confirmation_action == "Start charging measurement"
    persisted = storage.load_current()
    assert persisted is not None
    assert persisted.confirmation_message == coordinator.current.confirmation_message
    assert persisted.confirmation_action == coordinator.current.confirmation_action

    confirmed = coordinator.confirm(session.id)
    assert confirmed.state == SessionState.RUNNING
    assert confirmed.phase == "Starting measurement"
    assert confirmed.confirmation_message is None
    assert confirmed.confirmation_action is None
    assert continued.wait(1)
    wait_for_state(coordinator, SessionState.COMPLETED)


def _completed_light_session(
    storage: SessionStorage,
    session_id: str,
    *,
    updated_at: str,
    rows: str = "bri,watt\n1,1.0\n",
    model_json: bool = True,
) -> None:
    snapshot = SessionSnapshot(
        id=session_id,
        state=SessionState.COMPLETED,
        created_at="2026-09-01T12:00:00Z",
        updated_at=updated_at,
    )
    storage.create(snapshot, light_request(), set_current=False)
    output = storage.artifact_directory(session_id, "LCT010")
    output.mkdir()
    (output / "brightness.csv").write_text(rows, encoding="utf-8")
    if model_json:
        (output / "model.json").write_text(
            '{"name":"Test light","standby_power":0.2,"measure_settings":{"VERSION":"test"}}',
            encoding="utf-8",
        )


def test_merge_creates_a_completed_session_without_taking_current(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    _completed_light_session(storage, "left-session", updated_at="2026-09-01T12:00:00Z")
    _completed_light_session(
        storage,
        "right-session",
        updated_at="2026-09-02T12:00:00Z",
        rows="bri,watt\n1,9.0\n2,2.0\n",
    )
    running = MeasurementCoordinator(storage, CompletingService)
    live = running.start(light_request())
    wait_for_state(running, SessionState.COMPLETED)

    merged = running.merge("left-session", "right-session")

    assert merged.state == SessionState.COMPLETED
    assert merged.id not in {live.id, "left-session", "right-session"}
    assert running.current is not None
    assert running.current.id == live.id
    assert "id" in (tmp_path / "current.json").read_text(encoding="utf-8")
    assert live.id in (tmp_path / "current.json").read_text(encoding="utf-8")
    request = storage.load_request(merged.id)
    assert request.derived_from == ("left-session", "right-session")
    csv_text = (storage.artifact_directory(merged.id, "LCT010") / "brightness.csv").read_text(encoding="utf-8")
    assert "2,2.0" in csv_text
    model = (storage.artifact_directory(merged.id, "LCT010") / "model.json").read_text(encoding="utf-8")
    assert "MERGED_FROM" in model


def test_merge_preview_reports_added_and_kept(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    _completed_light_session(storage, "left-session", updated_at="2026-09-01T12:00:00Z")
    _completed_light_session(
        storage,
        "right-session",
        updated_at="2026-09-01T12:00:00Z",
        rows="bri,watt\n1,1.0\n2,2.0\n",
    )
    coordinator = MeasurementCoordinator(storage, CompletingService)

    preview = coordinator.preview_merge("left-session", "right-session")

    assert preview["model_id_warning"] is False
    assert preview["modes"]["brightness"]["kept"] == 1
    assert preview["modes"]["brightness"]["added"] == 1


def test_merge_rejects_a_session_without_lut_rows(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    _completed_light_session(storage, "left-session", updated_at="2026-09-01T12:00:00Z")
    empty = SessionSnapshot(
        id="empty-session",
        state=SessionState.COMPLETED,
        created_at="2026-09-01T12:00:00Z",
        updated_at="2026-09-01T12:00:00Z",
    )
    storage.create(empty, light_request(), set_current=False)
    coordinator = MeasurementCoordinator(storage, CompletingService)

    with pytest.raises(MergeEligibilityError, match="complete LUT row"):
        coordinator.merge("left-session", "empty-session")


class ExistingDirService(SessionMeasurementService):
    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult:
        context.artifact_directory.mkdir(parents=True, exist_ok=True)
        (context.artifact_directory / "brightness.csv").write_text("bri,watt\n1,1.0\n2,2.0\n", encoding="utf-8")
        control.progress(completed=1, total=1, mode="brightness", estimated_remaining="0s")
        return RunnerResult(model_json_data={"device_type": "light"})


def test_extend_start_copies_seed_artifacts_and_marks_current(tmp_path: Path) -> None:
    storage = SessionStorage(tmp_path)
    _completed_light_session(storage, "seed-session", updated_at="2026-09-01T12:00:00Z")
    coordinator = MeasurementCoordinator(storage, ExistingDirService)

    session = coordinator.start(
        light_request().model_copy(
            update={"resume_policy": ResumePolicy.EXTEND, "seed_session_id": "seed-session"},
        ),
    )
    wait_for_state(coordinator, SessionState.COMPLETED)

    assert coordinator.current is not None
    assert coordinator.current.id == session.id
    assert session.id in (tmp_path / "current.json").read_text(encoding="utf-8")
    copied = storage.artifact_directory(session.id, "LCT010") / "brightness.csv"
    assert copied.is_file()
