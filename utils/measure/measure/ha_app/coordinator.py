from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
import json
import logging
from pathlib import Path
from threading import Lock, Thread
import time
from typing import Protocol, cast
from uuid import uuid4

from measure.analyser.execution import RecorderAnalysisExecution
from measure.clock import utc_now
from measure.controller.light.const import LutMode
from measure.execution import MeasurementCancelledError, OperatingPoint
from measure.ha_app.session import (
    ACTIVE_SESSION_STATES,
    RESUMABLE_SESSION_STATES,
    CalibrationSample,
    SessionControl,
    SessionEvent,
    SessionEventType,
    SessionSnapshot,
    SessionState,
)
from measure.ha_app.storage import SESSION_LOAD_ERRORS, SessionStorage
from measure.model import write_model_json
from measure.request import LightMeasurementRequest, MeasurementRequest, RecorderMeasurementRequest, ResumePolicy
from measure.runner.light_plan import CSV_HEADERS
from measure.runner.lut_csv import (
    ModeUnionPreview,
    concatenate_raw_jsonl,
    load_mode_rows,
    preview_union,
    union_rows,
    write_mode_csv,
)
from measure.runner.runner import RunnerResult

_LOGGER = logging.getLogger("measure")
_SNAPSHOT_PERSIST_INTERVAL = 5.0
_ANALYSIS_WARNING_PREFIXES = (
    "Profile was not created:",
    "Profile model was not created:",
    "Recording analysis:",
    "Recording analysis failed:",
)


class SessionConflictError(Exception):
    """Raised when an operation conflicts with the active session state."""


class MergeEligibilityError(Exception):
    """Raised when two sessions cannot be merged."""


@dataclass(frozen=True)
class SessionExecutionContext:
    """Explicit identity and artifact location for one session execution."""

    session_id: str
    artifact_directory: Path


class SessionMeasurementService(Protocol):
    """Run one session without exposing its adapter composition to the coordinator."""

    def run(
        self,
        request: MeasurementRequest,
        control: SessionControl,
        context: SessionExecutionContext,
    ) -> RunnerResult: ...


class MeasurementCoordinator:
    """Own the Home Assistant measurement sessions and the single slot they compete for.

    State transitions are serialized under a lock while the measurement runs on a worker thread.
    """

    def __init__(self, storage: SessionStorage, service_factory: Callable[[], SessionMeasurementService]) -> None:
        self.storage = storage
        self.service_factory = service_factory
        self._lock = Lock()
        self._snapshot = storage.load_current()
        self._events = list(storage.load_events(self._snapshot.id)) if self._snapshot is not None else []
        self._last_snapshot_write = 0.0
        self._control: SessionControl | None = None
        self._worker: Thread | None = None
        self._analysing: set[str] = set()
        self._listeners: list[Callable[[], None]] = []

    @property
    def current(self) -> SessionSnapshot | None:
        with self._lock:
            return self._snapshot

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Notify a listener whenever the externally visible session state changes."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _notify_listeners(self) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:  # Observers must not affect measurement execution.
                _LOGGER.exception("Measurement session state listener failed")

    def get(self, session_id: str) -> SessionSnapshot:
        """Return the live projection or a stored historical snapshot."""
        with self._lock:
            return self._snapshot_locked(session_id)

    def _snapshot_locked(self, session_id: str) -> SessionSnapshot:
        """Resolve one session against the live projection first. Call while holding the lock."""

        if self._snapshot is not None and self._snapshot.id == session_id:
            return self._snapshot
        return self.storage.load_snapshot(session_id)

    def sessions(self) -> tuple[SessionSnapshot, ...]:
        """Return all retained sessions with the live projection substituted."""
        stored = self.storage.list_sessions()
        with self._lock:
            current = self._snapshot
            if current is None:
                return stored
            return tuple(current if snapshot.id == current.id else snapshot for snapshot in stored)

    def start(self, request: MeasurementRequest) -> SessionSnapshot:
        """Persist and launch a new session, rejecting overlapping work."""

        with self._lock:
            if self._snapshot and self._snapshot.state in ACTIVE_SESSION_STATES:
                raise SessionConflictError("A measurement session is already active")
            if self._analysing:
                raise SessionConflictError("Recording analysis is already active")
            if request.resume_policy == ResumePolicy.RESUME:
                raise SessionConflictError("Use the resume action for persisted output")
            if request.resume_policy == ResumePolicy.EXTEND:
                self._require_extend_seed(request)
            now = utc_now()
            snapshot = SessionSnapshot(
                id=str(uuid4()),
                state=SessionState.READY,
                created_at=now,
                updated_at=now,
            )
            self.storage.create(snapshot, request)
            if request.resume_policy == ResumePolicy.EXTEND and request.seed_session_id:
                self.storage.copy_lut_artifacts(request.seed_session_id, snapshot.id, request.model_id)
            self._snapshot = snapshot
            self._events = []
            self._last_snapshot_write = 0.0
            self._launch_locked(request)
            snapshot = self._snapshot
        self._notify_listeners()
        return snapshot

    def resume(self, session_id: str) -> SessionSnapshot:
        """Relaunch a retained session from compatible persisted output."""

        with self._lock:
            if self._snapshot is not None and self._snapshot.state in ACTIVE_SESSION_STATES:
                raise SessionConflictError("A measurement session is already active")
            if self._analysing:
                raise SessionConflictError("Recording analysis is already active")
            try:
                snapshot = self._snapshot_locked(session_id)
            except SESSION_LOAD_ERRORS as error:
                raise SessionConflictError("The requested session does not exist") from error
            if snapshot.state not in RESUMABLE_SESSION_STATES:
                raise SessionConflictError("The requested session cannot be resumed")
            if not self.storage.can_resume(snapshot.id) and not self.storage.can_relaunch(snapshot.id):
                raise SessionConflictError("The requested session has no compatible request to resume")
            self._snapshot = snapshot
            self._events = list(self.storage.load_events(snapshot.id))
            self.storage.set_current(snapshot.id)
            request = self.storage.load_request(snapshot.id)
            # A crashed NEW session must become RESUME so the runner continues from
            # the last cartesian row. EXTEND must stay EXTEND: the copied seed (and
            # any refine progress) is a union that missing_variations can subtract
            # from, but variations_after cannot look up.
            if request.resume_policy != ResumePolicy.EXTEND:
                request = request.model_copy(update={"resume_policy": ResumePolicy.RESUME})
            self._launch_locked(request)
            current = self._snapshot
        self._notify_listeners()
        return current

    def cancel(self, session_id: str) -> SessionSnapshot:
        """Persist cancellation intent and signal the worker cooperatively."""

        with self._lock:
            snapshot = self._require_active(session_id)
            if snapshot.state == SessionState.CANCELLED:
                return snapshot
            if snapshot.state not in {
                SessionState.RUNNING,
                SessionState.AWAITING_CONFIRMATION,
                SessionState.CANCELLING,
            }:
                raise SessionConflictError("No running measurement session")
            if snapshot.state != SessionState.CANCELLING:
                snapshot = replace(
                    snapshot,
                    state=SessionState.CANCELLING,
                    phase="Stopping measurement",
                    confirmation_message=None,
                    confirmation_action=None,
                    wait_ends_at=None,
                    wait_seconds=None,
                    updated_at=utc_now(),
                )
                self._snapshot = snapshot
                self.storage.write_snapshot(snapshot)
            if self._control is not None:
                self._control.cancel()
        self._notify_listeners()
        return snapshot

    def confirm(self, session_id: str) -> SessionSnapshot:
        """Release a worker paused at an operator checkpoint."""

        with self._lock:
            snapshot = self._require_active(session_id)
            if snapshot.state != SessionState.AWAITING_CONFIRMATION:
                raise SessionConflictError("The requested session is not waiting for confirmation")
            if self._control is None:
                raise SessionConflictError("The requested session cannot be continued")
            running: SessionSnapshot = replace(
                snapshot,
                state=SessionState.RUNNING,
                phase="Starting measurement",
                confirmation_message=None,
                confirmation_action=None,
                wait_ends_at=None,
                wait_seconds=None,
                updated_at=utc_now(),
            )
            self._snapshot = running
            self.storage.write_snapshot(running)
            self._control.continue_run()
        self._notify_listeners()
        return running

    def request_shutdown_stop(self, timeout: float) -> None:
        """Best-effort cooperative stop, called from the app's own shutdown hook.

        A measurement is meant to run unattended for hours; the process it runs in can
        still be stopped out from under it at any moment for reasons that have nothing to
        do with the measurement -- an add-on update, a Supervisor cold-backup stopping
        every add-on before it snapshots the filesystem, an OOM kill elsewhere freeing
        memory. None of those are a reason to lose progress. Stopping cooperatively (the
        same path the Stop button uses) gives the worker a chance to finish writing its
        current row and land in the CANCELLED state -- which is always resumable -- instead
        of being abandoned mid-write when the process actually exits a moment later.

        This is genuinely best-effort: if the platform sends SIGKILL before ``timeout``
        elapses (some fixed grace period the platform enforces, not this method), nothing
        here runs at all and the next boot falls back to the ordinary crash-recovery path
        in ``SessionStorage.load_current``.
        """
        with self._lock:
            snapshot = self._snapshot
            worker = self._worker
        if snapshot is None or snapshot.state not in ACTIVE_SESSION_STATES:
            return
        with suppress(SessionConflictError):
            self.cancel(snapshot.id)
        if worker is not None:
            worker.join(timeout=timeout)

    def delete(self, session_id: str) -> None:
        """Delete a terminal retained session."""
        with self._lock:
            try:
                snapshot = self._snapshot_locked(session_id)
            except SESSION_LOAD_ERRORS as error:
                raise SessionConflictError("The requested session does not exist") from error
            if snapshot.state in ACTIVE_SESSION_STATES:
                raise SessionConflictError("An active measurement session cannot be deleted")
            if session_id in self._analysing:
                raise SessionConflictError("A session cannot be deleted while its recording is being analysed")
            self.storage.delete_session(session_id)
            if self._snapshot is not None and self._snapshot.id == session_id:
                self._snapshot = None
                self._events = []
        self._notify_listeners()

    def preview_merge(self, left_id: str, right_id: str) -> dict[str, object]:
        """Return per-mode kept/added/replaced counts without writing a session."""
        left_request, right_request, left_rows, right_rows = self._merge_sources(left_id, right_id)
        modes = {
            mode.value: {
                "kept": preview.kept,
                "added": preview.added,
                "replaced": preview.replaced,
                "replaced_reasons": preview.replaced_reasons,
            }
            for mode, preview in self._merge_mode_previews(left_rows, right_rows).items()
        }
        return {
            "left": left_id,
            "right": right_id,
            "model_id_warning": left_request.model_id != right_request.model_id,
            "modes": modes,
        }

    def merge(self, left_id: str, right_id: str) -> SessionSnapshot:
        """Create a completed union session without taking the measurement slot."""
        left_request, right_request, left_rows, right_rows = self._merge_sources(left_id, right_id)
        left_snapshot = self.storage.load_snapshot(left_id)
        right_snapshot = self.storage.load_snapshot(right_id)
        later_id = right_id if right_snapshot.updated_at >= left_snapshot.updated_at else left_id
        later_request = right_request if later_id == right_id else left_request
        earlier_id = left_id if later_id == right_id else right_id
        merged_request = later_request.model_copy(
            update={
                "modes": left_request.modes | right_request.modes,
                "derived_from": (left_id, right_id),
                "resume_policy": ResumePolicy.NEW,
                "seed_session_id": None,
                "remeasure_existing": False,
            },
        )
        now = utc_now()
        snapshot = SessionSnapshot(
            id=str(uuid4()),
            state=SessionState.COMPLETED,
            created_at=now,
            updated_at=now,
            phase="Measurement completed",
        )
        with self._lock:
            self.storage.create(snapshot, merged_request, set_current=False)
        self._write_merge_artifacts(
            snapshot.id,
            merged_request,
            later_id,
            earlier_id,
            left_id,
            right_id,
            left_rows,
            right_rows,
        )
        files = self.storage.list_files(snapshot.id)
        total = sum(len(union_rows(left_rows.get(mode, {}), right_rows.get(mode, {}))) for mode in CSV_HEADERS)
        snapshot = replace(snapshot, files=files, completed=total, total=total, updated_at=utc_now())
        self.storage.write_snapshot(snapshot)
        return snapshot

    def _require_extend_seed(self, request: MeasurementRequest) -> None:
        if not isinstance(request, LightMeasurementRequest) or not request.seed_session_id:
            raise SessionConflictError("Extend requires a light seed session")
        try:
            seed = self.storage.load_request(request.seed_session_id)
        except SESSION_LOAD_ERRORS as error:
            raise SessionConflictError("The seed session does not exist") from error
        if not isinstance(seed, LightMeasurementRequest):
            raise SessionConflictError("The seed session is not a light measurement")
        if not self.storage.has_complete_lut_row(request.seed_session_id):
            raise SessionConflictError("The seed session has no complete LUT row to extend")

    def _merge_sources(
        self,
        left_id: str,
        right_id: str,
    ) -> tuple[
        LightMeasurementRequest,
        LightMeasurementRequest,
        dict[LutMode, dict],
        dict[LutMode, dict],
    ]:
        if left_id == right_id:
            raise MergeEligibilityError("A session cannot be merged with itself")
        left_request = self._require_mergeable_request(left_id)
        right_request = self._require_mergeable_request(right_id)
        with self._lock:
            current = self._snapshot
        for session_id in (left_id, right_id):
            if current is not None and current.id == session_id and current.state in ACTIVE_SESSION_STATES:
                raise SessionConflictError("An active measurement session cannot be merged")
        left_rows = self._load_session_lut_rows(left_id, left_request)
        right_rows = self._load_session_lut_rows(right_id, right_request)
        if not any(left_rows.values()) or not any(right_rows.values()):
            raise MergeEligibilityError("Each session must have at least one complete LUT row")
        return left_request, right_request, left_rows, right_rows

    def _require_mergeable_request(self, session_id: str) -> LightMeasurementRequest:
        try:
            request = self.storage.load_request(session_id)
        except SESSION_LOAD_ERRORS as error:
            raise MergeEligibilityError("The requested session does not exist") from error
        if not isinstance(request, LightMeasurementRequest):
            raise MergeEligibilityError("Only light sessions can be merged")
        return request

    def _load_session_lut_rows(
        self,
        session_id: str,
        request: LightMeasurementRequest,
    ) -> dict[LutMode, dict]:
        artifact = self.storage.artifact_directory(session_id, request.model_id)
        snapshot = self.storage.load_snapshot(session_id)
        return {
            mode: load_mode_rows(
                artifact / f"{mode.value}.csv",
                mode,
                session_updated_at=snapshot.updated_at,
            )
            for mode in CSV_HEADERS
        }

    @staticmethod
    def _merge_mode_previews(
        left_rows: dict[LutMode, dict],
        right_rows: dict[LutMode, dict],
    ) -> dict[LutMode, ModeUnionPreview]:
        previews: dict[LutMode, ModeUnionPreview] = {}
        for mode in CSV_HEADERS:
            left = left_rows.get(mode, {})
            right = right_rows.get(mode, {})
            if not left and not right:
                continue
            previews[mode] = preview_union(left, right)
        return previews

    def _write_merge_artifacts(
        self,
        session_id: str,
        request: LightMeasurementRequest,
        later_id: str,
        earlier_id: str,
        left_id: str,
        right_id: str,
        left_rows: dict[LutMode, dict],
        right_rows: dict[LutMode, dict],
    ) -> None:
        destination = self.storage.artifact_directory(session_id, request.model_id)
        destination.mkdir(parents=True, exist_ok=True)
        later_request = self.storage.load_request(later_id)
        earlier_request = self.storage.load_request(earlier_id)
        assert isinstance(later_request, LightMeasurementRequest)
        assert isinstance(earlier_request, LightMeasurementRequest)
        later_artifact = self.storage.artifact_directory(later_id, later_request.model_id)
        earlier_artifact = self.storage.artifact_directory(earlier_id, earlier_request.model_id)
        for mode in CSV_HEADERS:
            merged = union_rows(left_rows.get(mode, {}), right_rows.get(mode, {}))
            if not merged:
                continue
            write_mode_csv(destination / f"{mode.value}.csv", mode, merged)
            concatenate_raw_jsonl(
                destination / f"{mode.value}.raw.jsonl",
                earlier_artifact / f"{mode.value}.raw.jsonl",
                later_artifact / f"{mode.value}.raw.jsonl",
            )
        self._write_merge_model_json(destination, request, later_artifact, earlier_artifact, left_id, right_id)

    def _write_merge_model_json(
        self,
        destination: Path,
        request: LightMeasurementRequest,
        later_artifact: Path,
        earlier_artifact: Path,
        left_id: str,
        right_id: str,
    ) -> None:
        source = later_artifact / "model.json"
        if not source.is_file():
            source = earlier_artifact / "model.json"
        if source.is_file():
            data = json.loads(source.read_text(encoding="utf-8"))
            data["created_at"] = utc_now()
            settings = data.get("measure_settings")
            if not isinstance(settings, dict):
                settings = {}
                data["measure_settings"] = settings
            settings["MERGED_FROM"] = [left_id, right_id]
            (destination / "model.json").write_text(
                json.dumps(data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return
        write_model_json(
            destination,
            standby_power=0,
            name=request.model_name or request.model_id,
            measure_device=request.measure_device,
            parameters=request.parameters,
            extra_json_data={"device_type": "light", "calculation_strategy": "lut"},
            extra_measure_settings={"MERGED_FROM": [left_id, right_id]},
            num_lights=request.multiple_light_count,
            power_meter=request.power_meter,
        )

    def analyse(self, session_id: str) -> SessionSnapshot:
        """Rebuild analysis and profile output from a retained raw recording."""

        with self._lock:
            if self._snapshot is not None and self._snapshot.state in ACTIVE_SESSION_STATES:
                raise SessionConflictError("A measurement session is already active")
            try:
                snapshot = self._snapshot_locked(session_id)
            except SESSION_LOAD_ERRORS as error:
                raise SessionConflictError("The requested session does not exist") from error
            if session_id in self._analysing:
                raise SessionConflictError("The requested recording is already being analysed")
            if snapshot.state in ACTIVE_SESSION_STATES or not self.storage.can_analyse(session_id):
                raise SessionConflictError("The requested session has no recording that can be analysed")
            request = self.storage.load_request(session_id)
            if not isinstance(request, RecorderMeasurementRequest):  # pragma: no cover - guarded by can_analyse
                raise SessionConflictError("The requested session is not a recorder session")
            self._analysing.add(session_id)

        try:
            summary = RecorderAnalysisExecution().run(
                request,
                self.storage.artifact_directory(session_id, request.model_id),
                summary=snapshot.summary,
            )
            with self._lock:
                updated = replace(
                    snapshot,
                    updated_at=utc_now(),
                    files=self.storage.list_files(session_id),
                    summary=summary,
                    warnings=tuple(
                        warning for warning in snapshot.warnings if not warning.startswith(_ANALYSIS_WARNING_PREFIXES)
                    ),
                )
                self.storage.write_snapshot(updated)
                if self._snapshot is not None and self._snapshot.id == session_id:
                    self._snapshot = updated
        finally:
            with self._lock:
                self._analysing.discard(session_id)
        self._notify_listeners()
        return updated

    def _require_active(self, session_id: str) -> SessionSnapshot:
        """Return the live projection, refusing any session that does not hold the measurement slot."""

        if self._snapshot is None or self._snapshot.id != session_id:
            raise SessionConflictError("The requested session is not active")
        return self._snapshot

    def events_since(self, sequence: int, session_id: str) -> tuple[SessionEvent, ...]:
        """Return events after ``sequence`` for a live or retained session."""
        with self._lock:
            if self._snapshot is not None and self._snapshot.id == session_id:
                return tuple(event for event in self._events if event.sequence > sequence)
        return tuple(event for event in self.storage.load_events(session_id) if event.sequence > sequence)

    def _launch_locked(self, request: MeasurementRequest) -> None:
        """Create session control and launch the worker while holding the coordinator lock."""

        assert self._snapshot is not None
        self._control = SessionControl(initial_sequence=self._snapshot.event_sequence)
        self._control.subscribe(self._handle_event)
        self._snapshot = replace(
            self._snapshot,
            state=SessionState.RUNNING,
            phase="Initializing measurement",
            confirmation_message=None,
            confirmation_action=None,
            calibration_sample=None,
            run_started_at=None,
            wait_ends_at=None,
            wait_seconds=None,
            updated_at=utc_now(),
            error=None,
            warnings=(),
        )
        self.storage.write_snapshot(self._snapshot)
        session_id = self._snapshot.id
        self._worker = Thread(
            target=self._run,
            args=(session_id, request, self._control),
            name=f"measure-{session_id[:8]}",
            daemon=True,
        )
        self._worker.start()

    def _run(
        self,
        session_id: str,
        request: MeasurementRequest,
        control: SessionControl,
    ) -> None:
        try:
            context = SessionExecutionContext(
                session_id=session_id,
                artifact_directory=self.storage.artifact_directory(session_id, request.model_id),
            )
            result = self.service_factory().run(
                request,
                control,
                context,
            )
        except MeasurementCancelledError:
            self._finish(SessionState.CANCELLED)
        except Exception as error:
            self._finish(SessionState.FAILED, error=str(error))
            _LOGGER.exception("Measurement session %s failed", session_id)
        else:
            self._finish(SessionState.COMPLETED, summary=result.summary)

    def _handle_event(self, event: SessionEvent) -> None:
        """Project runner events onto the snapshot and persistence policy."""

        with self._lock:
            if self._snapshot is None:
                return
            self._events.append(event)
            if len(self._events) > 1000:
                self._events = self._events[-1000:]
            if self._project_transient_sample(event):
                return
            if event.type == SessionEventType.PROGRESS:
                self._snapshot = replace(
                    self._snapshot,
                    event_sequence=event.sequence,
                    updated_at=event.created_at,
                    completed=int(event.data["completed"]),
                    total=int(event.data["total"]),
                    skipped=int(event.data.get("skipped", 0)),
                    already_measured=int(event.data.get("already_measured", 0)),
                    phase=str(event.data["mode"]),
                    mode=str(event.data["mode"]),
                    estimated_remaining=str(event.data["estimated_remaining"]),
                    run_started_at=self._snapshot.run_started_at or event.created_at,
                    wait_ends_at=None,
                    wait_seconds=None,
                    # A later accepted sample means whatever was in the banner has
                    # already been recovered from (or was never blocking). Leaving it
                    # up trains the operator to ignore the banner; the message itself
                    # stays in the log feed either way.
                    warnings=(),
                )
            elif event.type == SessionEventType.PHASE:
                self._snapshot = self._phase_snapshot(event)
            elif event.type == SessionEventType.OPERATING_POINT:
                self._snapshot = replace(
                    self._snapshot,
                    event_sequence=event.sequence,
                    updated_at=event.created_at,
                    operating_point=cast(OperatingPoint, event.data),
                    warnings=(),
                )
            elif event.type == SessionEventType.WARNING:
                self._snapshot = replace(
                    self._snapshot,
                    event_sequence=event.sequence,
                    updated_at=event.created_at,
                    warnings=self._append_warning(
                        self._snapshot.warnings,
                        str(event.data["message"]),
                    ),
                )
            elif event.type == SessionEventType.CHECKPOINT:
                self._snapshot = replace(
                    self._snapshot,
                    event_sequence=event.sequence,
                    updated_at=event.created_at,
                    state=SessionState.AWAITING_CONFIRMATION,
                    phase="Waiting for confirmation",
                    confirmation_message=str(event.data["message"]),
                    confirmation_action=(str(event.data["action"]) if event.data.get("action") else None),
                    wait_ends_at=None,
                    wait_seconds=None,
                )
            else:
                self._snapshot = replace(
                    self._snapshot,
                    event_sequence=event.sequence,
                    updated_at=event.created_at,
                )
            durable = event.type in {
                SessionEventType.STATE,
                SessionEventType.PHASE,
                SessionEventType.WARNING,
                SessionEventType.CHECKPOINT,
            }
            self.storage.append_event(self._snapshot.id, event, durable=durable)
            if self._should_persist_snapshot(event):
                self.storage.write_snapshot(self._snapshot)
                self._last_snapshot_write = time.monotonic()
        self._notify_checkpoint(event)

    @staticmethod
    def _append_warning(warnings: tuple[str, ...], warning: str) -> tuple[str, ...]:
        """Append a user-facing warning, collapsing duplicates and keeping the last 20."""

        return tuple(dict.fromkeys((*warnings, warning)))[-20:]

    def _phase_snapshot(self, event: SessionEvent) -> SessionSnapshot:
        """Apply a PHASE event. ``reason`` is only replaced when the event carries one."""

        assert self._snapshot is not None
        wait_seconds = event.data.get("wait_seconds")
        wait_ends_at = event.data.get("wait_ends_at")
        activity_reason = self._snapshot.activity_reason
        if "reason" in event.data:
            raw = event.data.get("reason")
            activity_reason = str(raw) if raw else None
        return replace(
            self._snapshot,
            event_sequence=event.sequence,
            updated_at=event.created_at,
            phase=str(event.data["message"]),
            wait_seconds=float(wait_seconds) if wait_seconds is not None else None,
            wait_ends_at=str(wait_ends_at) if wait_ends_at else None,
            activity_reason=activity_reason,
        )

    def _notify_checkpoint(self, event: SessionEvent) -> None:
        """Publish the state transition caused by an operator checkpoint."""
        if event.type == SessionEventType.CHECKPOINT:
            self._notify_listeners()

    def _project_transient_sample(self, event: SessionEvent) -> bool:
        """Project live readings in memory without writing high-frequency snapshots."""

        assert self._snapshot is not None
        if event.type == SessionEventType.SAMPLE:
            self._snapshot = replace(self._snapshot, event_sequence=event.sequence, updated_at=event.created_at)
        elif event.type == SessionEventType.CALIBRATION_SAMPLE:
            self._snapshot = replace(
                self._snapshot,
                event_sequence=event.sequence,
                updated_at=event.created_at,
                calibration_sample=cast(CalibrationSample, event.data),
            )
        elif event.type == SessionEventType.ENTITY_STATES:
            self._snapshot = replace(
                self._snapshot,
                event_sequence=event.sequence,
                updated_at=event.created_at,
                entity_states={str(key): str(value) for key, value in event.data.get("states", {}).items()},
            )
        else:
            return False
        return True

    def _should_persist_snapshot(self, event: SessionEvent) -> bool:
        if event.type == SessionEventType.LOG:
            return False
        if event.type != SessionEventType.PROGRESS:
            return True
        return time.monotonic() - self._last_snapshot_write >= _SNAPSHOT_PERSIST_INTERVAL

    def _finish(self, state: SessionState, error: str | None = None, summary: dict[str, str] | None = None) -> None:
        """Persist the terminal snapshot and its final state event."""

        with self._lock:
            if self._snapshot is None:
                return
            files = self.storage.list_files(self._snapshot.id)
            updated_at = utc_now()
            sequence = (
                max(
                    self._snapshot.event_sequence,
                    self._control.sequence if self._control is not None else 0,
                )
                + 1
            )
            self._snapshot = replace(
                self._snapshot,
                state=state,
                phase={
                    SessionState.CANCELLED: "Measurement stopped",
                    SessionState.COMPLETED: "Measurement completed",
                    SessionState.FAILED: "Measurement failed",
                }.get(state, self._snapshot.phase),
                confirmation_message=None,
                confirmation_action=None,
                wait_ends_at=None,
                wait_seconds=None,
                updated_at=updated_at,
                error=error,
                files=files,
                event_sequence=sequence,
                summary=summary if summary is not None else self._snapshot.summary,
            )
            event = SessionEvent(
                sequence=sequence,
                type=SessionEventType.STATE,
                created_at=updated_at,
                data={"state": state, "error": error},
            )
            self._events.append(event)
            self.storage.append_event(self._snapshot.id, event)
            self.storage.write_snapshot(self._snapshot)
        self._notify_listeners()
