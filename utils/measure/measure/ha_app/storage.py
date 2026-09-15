from collections import deque
import csv
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from measure.clock import utc_now
from measure.controller.light.const import MAX_MIRED, MIN_MIRED, LutMode
from measure.controller.light.controller import LightInfo
from measure.dummy_load import DummyLoadCalibration
from measure.files import write_json_atomic
from measure.ha_app.preferences import AppPreferences

if TYPE_CHECKING:
    from measure.ha_app.contribution.models import ContributionStatus
from measure.ha_app.session import (
    ACTIVE_SESSION_STATES,
    SessionEvent,
    SessionEventType,
    SessionSnapshot,
    SessionState,
)
from measure.ha_app.shelly_credentials import ShellyCredentials, ShellyCredentialStore
from measure.request import (
    LightMeasurementRequest,
    MeasurementRequest,
    RecorderMeasurementRequest,
    RecorderPurpose,
    ResumePolicy,
    parse_measurement_request,
)
from measure.runner.light_plan import (
    CSV_HEADERS,
    ColorTempVariation,
    EffectVariation,
    HsVariation,
    Variation,
    build_light_plan,
    variation_from_csv_row,
)
from measure.runner.lut_csv import load_session_measured_variations
from measure.runner.on_off_bounds import ON_OFF_BOUNDS_FILENAME

_LOGGER = logging.getLogger("measure")

_CURRENT_SESSION_FILENAME = "current.json"
_DUMMY_LOAD_CALIBRATION_FILENAME = "dummy_load_calibration.json"
_CONTRIBUTION_STATUS_FILENAME = "contribution_status.json"
_SHELLY_CREDENTIALS_FILENAME = "shelly_credentials.json"

#: Everything reading a persisted session document can raise: the directory is gone or
#: unreadable, or the JSON no longer matches the model that wrote it. Callers treat all of
#: these the same way — the session cannot be loaded — so they are caught as one set.
SESSION_LOAD_ERRORS = (OSError, KeyError, TypeError, ValueError)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class SessionStorage:
    """Persist session state and outputs below a confined data root."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.sessions_root = self.data_root / "sessions"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        # Requests are immutable per session; cache them so hot paths (SSE encoding)
        # do not re-read and re-validate the JSON document from disk.
        self._request_cache: dict[str, MeasurementRequest] = {}
        self._shelly_credentials = ShellyCredentialStore(self.data_root / _SHELLY_CREDENTIALS_FILENAME)

    def session_directory(self, session_id: str) -> Path:
        if not session_id or not session_id.replace("-", "").isalnum():
            raise ValueError("Invalid session id")
        return self._contained(self.sessions_root / session_id)

    def create(
        self,
        snapshot: SessionSnapshot,
        request: MeasurementRequest,
        *,
        set_current: bool = True,
    ) -> Path:
        """Create a session layout and optionally mark it as current.

        Merge writes a completed session without taking the measurement slot, so it
        must not overwrite ``current.json`` or steal crash-recovery from a running run.
        """

        directory = self.session_directory(snapshot.id)
        (directory / "output").mkdir(parents=True, exist_ok=False)
        self._write_json(directory / "request.json", request.model_dump(mode="json"))
        self._request_cache[snapshot.id] = request
        self.write_snapshot(snapshot)
        if set_current:
            self._write_json(self.data_root / _CURRENT_SESSION_FILENAME, {"id": snapshot.id})
        return directory

    def set_current(self, session_id: str) -> None:
        """Point startup recovery at an existing session."""
        self.load_snapshot(session_id)
        self._write_json(self.data_root / _CURRENT_SESSION_FILENAME, {"id": session_id})

    def clear_current(self, session_id: str | None = None) -> None:
        """Remove the current pointer, optionally only when it names ``session_id``."""
        path = self.data_root / _CURRENT_SESSION_FILENAME
        if session_id is not None and path.exists():
            try:
                if str(self._read_json(path)["id"]) != session_id:
                    return
            except SESSION_LOAD_ERRORS:
                pass
        path.unlink(missing_ok=True)

    def delete_session(self, session_id: str) -> None:
        self._request_cache.pop(session_id, None)
        directory = self.session_directory(session_id)
        if directory.exists():
            shutil.rmtree(directory)
        self.clear_current(session_id)

    def write_snapshot(self, snapshot: SessionSnapshot) -> None:
        directory = self.session_directory(snapshot.id)
        directory.mkdir(parents=True, exist_ok=True)
        self._write_json(directory / "state.json", snapshot.to_dict())

    def append_event(self, session_id: str, event: SessionEvent, *, durable: bool = True) -> None:
        """Append an event, fsyncing only when durability is required."""

        path = self.session_directory(session_id) / "events.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event.to_dict(), separators=(",", ":"), default=str) + "\n")
            file.flush()
            if durable:
                os.fsync(file.fileno())

    def load_events(self, session_id: str, *, limit: int | None = 5000) -> tuple[SessionEvent, ...]:
        """Load persisted events, optionally retaining only the newest entries."""
        path = self.session_directory(session_id) / "events.jsonl"
        if not path.exists():
            return ()
        events: deque[SessionEvent] = deque(maxlen=limit)
        pending_line: str | None = None
        with path.open(encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                if pending_line is not None:
                    events.append(self._decode_event(pending_line))
                pending_line = line
        if pending_line is not None:
            try:
                events.append(self._decode_event(pending_line))
            except json.JSONDecodeError:
                # The session id is caller-supplied, so keep it out of the log and report the
                # recovered count instead — it says as much about where the file was cut off.
                _LOGGER.warning("Ignoring a truncated final session event after %d recovered events", len(events))
        return tuple(events)

    @staticmethod
    def _decode_event(line: str) -> SessionEvent:
        value = json.loads(line)
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(value, dict) or not isinstance(data, dict):
            raise ValueError("Persisted session event must be an object")
        return SessionEvent(
            sequence=int(value["sequence"]),
            type=SessionEventType(value["type"]),
            created_at=str(value["created_at"]),
            data=data,
        )

    def load_current(self) -> SessionSnapshot | None:
        """Load the current snapshot and recover an interrupted active session."""
        current_path = self.data_root / _CURRENT_SESSION_FILENAME
        if not current_path.exists():
            return None
        try:
            session_id = str(self._read_json(current_path)["id"])
            snapshot = self.load_snapshot(session_id)
        except SESSION_LOAD_ERRORS as error:
            _LOGGER.warning("Discarding incompatible current session pointer: %s", error)
            current_path.unlink(missing_ok=True)
            return None
        if snapshot.state in ACTIVE_SESSION_STATES:
            resumable = self.can_resume(snapshot.id)
            snapshot = replace(
                snapshot,
                state=SessionState.RESUMABLE if resumable else SessionState.FAILED,
                updated_at=utc_now(),
                error=(
                    "The app restarted during the measurement. Output is intact — press Resume to continue."
                    if resumable
                    else "The app restarted before a complete measurement row was saved."
                ),
            )
            self.write_snapshot(snapshot)
        return snapshot

    def load_snapshot(self, session_id: str) -> SessionSnapshot:
        """Load one persisted session without changing the current pointer."""
        self.load_request(session_id)
        state = self._read_json(self.session_directory(session_id) / "state.json")
        snapshot = SessionSnapshot(
            id=str(state["id"]),
            state=SessionState(state["state"]),
            created_at=str(state["created_at"]),
            updated_at=str(state["updated_at"]),
            completed=int(state.get("completed", 0)),
            total=int(state.get("total", 0)),
            skipped=int(state.get("skipped", 0)),
            already_measured=int(state.get("already_measured", 0)),
            phase=state.get("phase"),
            activity_reason=state.get("activity_reason"),
            confirmation_message=state.get("confirmation_message"),
            confirmation_action=state.get("confirmation_action"),
            mode=state.get("mode"),
            estimated_remaining=state.get("estimated_remaining"),
            run_started_at=state.get("run_started_at"),
            wait_ends_at=state.get("wait_ends_at"),
            wait_seconds=(float(state["wait_seconds"]) if state.get("wait_seconds") is not None else None),
            error=state.get("error"),
            files=tuple(state.get("files", [])),
            warnings=tuple(state.get("warnings", [])),
            event_sequence=int(state.get("event_sequence", 0)),
            summary=state.get("summary"),
            operating_point=state.get("operating_point"),
            calibration_sample=state.get("calibration_sample"),
        )
        if snapshot.id != session_id:
            raise ValueError("Session state id does not match its directory")
        return snapshot

    def list_sessions(self) -> tuple[SessionSnapshot, ...]:
        """Return valid persisted sessions, newest activity first."""
        sessions: list[SessionSnapshot] = []
        for path in self.sessions_root.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                sessions.append(self.load_snapshot(path.name))
            except SESSION_LOAD_ERRORS as error:
                _LOGGER.warning("Ignoring incompatible measurement session %s: %s", path.name, error)
        return tuple(sorted(sessions, key=lambda item: item.updated_at, reverse=True))

    def session_size(self, session_id: str) -> int:
        """Return the total size of regular files stored for one session."""
        return self.session_listing_stats(session_id)[1]

    def session_listing_stats(self, session_id: str) -> tuple[int, int]:
        """Return ``(output file count, total session bytes)`` from one walk."""

        directory = self.session_directory(session_id)
        if not directory.exists():
            raise FileNotFoundError(session_id)
        output = directory / "output"
        file_count = 0
        size = 0
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            size += path.stat().st_size
            if output in path.parents or path.parent == output:
                file_count += 1
        return file_count, size

    def has_lut_csv(self, session_id: str) -> bool:
        """True when any light LUT CSV has a data row. Does not parse the grid."""

        try:
            request = self.load_request(session_id)
        except SESSION_LOAD_ERRORS:
            return False
        if not isinstance(request, LightMeasurementRequest):
            return False
        root = self.artifact_directory(session_id, request.model_id)
        for mode in LutMode:
            path = root / f"{mode.value}.csv"
            if not path.is_file() or path.is_symlink():
                continue
            try:
                with path.open(encoding="utf-8", newline="") as handle:
                    next(handle, None)
                    if next(handle, None):
                        return True
            except OSError:
                continue
        return False

    def load_request(self, session_id: str) -> MeasurementRequest:
        cached = self._request_cache.get(session_id)
        if cached is not None:
            return cached
        path = self.session_directory(session_id) / "request.json"
        request = parse_measurement_request(self._read_json(path))
        self._request_cache[session_id] = request
        return request

    def output_directory(self, session_id: str) -> Path:
        return self._contained(self.session_directory(session_id) / "output")

    def artifact_directory(self, session_id: str, model_id: str) -> Path:
        """Return the confined output directory for one session model."""

        return self._contained(self.output_directory(session_id) / (model_id or "measurement"))

    def load_settings(self) -> AppPreferences:
        path = self.data_root / "settings.json"
        settings = self._load_model(path, AppPreferences, "persisted app settings; using defaults")
        return settings if settings is not None else AppPreferences()

    def save_settings(self, settings: AppPreferences) -> AppPreferences:
        self._write_json(self.data_root / "settings.json", settings.model_dump(mode="json"))
        return settings

    def load_shelly_credentials(self) -> ShellyCredentials | None:
        try:
            return self._shelly_credentials.load()
        except (OSError, ValueError) as error:
            _LOGGER.warning("Could not load persisted Shelly credentials: %s", error)
            return None

    def save_shelly_credentials(self, credentials: ShellyCredentials) -> None:
        self._shelly_credentials.save(credentials)

    def clear_shelly_credentials(self) -> None:
        self._shelly_credentials.clear()

    def load_dummy_load_calibration(self) -> DummyLoadCalibration | None:
        return self._load_model(
            self.data_root / _DUMMY_LOAD_CALIBRATION_FILENAME,
            DummyLoadCalibration,
            "dummy-load calibration",
        )

    def save_dummy_load_calibration(self, calibration: DummyLoadCalibration) -> DummyLoadCalibration:
        self._write_json(
            self.data_root / _DUMMY_LOAD_CALIBRATION_FILENAME,
            calibration.model_dump(mode="json"),
        )
        return calibration

    def load_session_dummy_load_calibration(self, session_id: str) -> DummyLoadCalibration | None:
        return self._load_model(
            self.session_directory(session_id) / _DUMMY_LOAD_CALIBRATION_FILENAME,
            DummyLoadCalibration,
            f"session dummy-load calibration for {session_id}",
        )

    def save_session_dummy_load_calibration(
        self,
        session_id: str,
        calibration: DummyLoadCalibration,
    ) -> DummyLoadCalibration:
        self._write_json(
            self.session_directory(session_id) / _DUMMY_LOAD_CALIBRATION_FILENAME,
            calibration.model_dump(mode="json"),
        )
        return calibration

    def load_contribution_status(self) -> "ContributionStatus":
        from measure.ha_app.contribution.models import ContributionStatus

        path = self.data_root / _CONTRIBUTION_STATUS_FILENAME
        status = self._load_model(path, ContributionStatus, "contribution status")
        return status if status is not None else ContributionStatus()

    def save_contribution_status(self, status: "ContributionStatus") -> None:
        self._write_json(self.data_root / _CONTRIBUTION_STATUS_FILENAME, status.model_dump(mode="json"))

    def can_resume(self, session_id: str) -> bool:
        """Return whether the session has a complete row matching its persisted request."""
        try:
            request = self.load_request(session_id)
        except SESSION_LOAD_ERRORS:
            return False
        if not isinstance(request, LightMeasurementRequest):
            return False
        # Refine copies the seed LUT. Its last row is usually not on the new
        # cartesian grid; resume only needs some complete row so missing_variations
        # can keep filling the plan.
        if request.resume_policy == ResumePolicy.EXTEND:
            return self.has_complete_lut_row(session_id)
        model_root = self.artifact_directory(session_id, request.model_id)
        for mode in request.modes:
            path = model_root / f"{mode.value}.csv"
            if self._has_complete_measurement_row(path, mode, request):
                return True
        return False

    def can_relaunch(self, session_id: str) -> bool:
        """Return whether the session can be started again from its stored request.

        Resume continues from the last complete LUT row. Relaunch is for a light
        session that never wrote one — typically a failed start — so the same
        session can be tried again without rebuilding the form.
        """
        try:
            request = self.load_request(session_id)
        except SESSION_LOAD_ERRORS:
            return False
        return isinstance(request, LightMeasurementRequest)

    def can_analyse(self, session_id: str) -> bool:
        """Return whether a retained complex-profile recording can be analysed again."""
        try:
            request = self.load_request(session_id)
        except SESSION_LOAD_ERRORS:
            return False
        if not isinstance(request, RecorderMeasurementRequest):
            return False
        if request.recorder_purpose != RecorderPurpose.COMPLEX_PROFILE:
            return False
        path = self.artifact_directory(session_id, request.model_id) / request.export_filename
        return path.is_file() and not path.is_symlink()

    def has_complete_lut_row(self, session_id: str) -> bool:
        """Return whether any complete LUT row exists, even if it is off the rebuilt plan.

        Merge and refine eligibility is weaker than crash-resume: any parseable row is
        enough to union or seed from.
        """
        try:
            request = self.load_request(session_id)
        except SESSION_LOAD_ERRORS:
            return False
        if not isinstance(request, LightMeasurementRequest):
            return False
        measured = load_session_measured_variations(self.artifact_directory(session_id, request.model_id))
        return any(measured.values())

    def copy_lut_artifacts(self, seed_session_id: str, dest_session_id: str, dest_model_id: str) -> None:
        """Copy seed LUT CSVs and raw logs into a new session's artifact directory."""
        seed_request = self.load_request(seed_session_id)
        if not isinstance(seed_request, LightMeasurementRequest):
            raise ValueError("Seed session is not a light measurement")
        source = self.artifact_directory(seed_session_id, seed_request.model_id)
        destination = self.artifact_directory(dest_session_id, dest_model_id)
        destination.mkdir(parents=True, exist_ok=True)
        if not source.is_dir():
            return
        for path in source.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith((".csv", ".csv.gz", ".raw.jsonl")) or path.name == ON_OFF_BOUNDS_FILENAME:
                shutil.copy2(path, destination / path.name)

    def verify_writable(self) -> None:
        """Exercise the same create/fsync/remove operations used by session persistence."""
        probe = self._contained(self.data_root / f".write-probe-{uuid4().hex}")
        try:
            with probe.open("x", encoding="utf-8") as file:
                file.write("ok")
                file.flush()
                os.fsync(file.fileno())
        finally:
            probe.unlink(missing_ok=True)

    def list_files(self, session_id: str) -> tuple[str, ...]:
        output = self.output_directory(session_id)
        if not output.exists():
            return ()
        # .as_posix(), not str() -- relative_to() keeps the OS-native separator, so on
        # Windows this would otherwise return "LCT010\\brightness.csv". Every consumer
        # (the plot lookup in measure.visualization, the frontend's file listing) matches
        # against a forward-slash key like "LCT010/brightness.csv", the only separator the
        # add-on's actual runtime (a Linux container) ever produces.
        return tuple(
            sorted(
                path.relative_to(output).as_posix()
                for path in output.rglob("*")
                if path.is_file() and not path.is_symlink()
            ),
        )

    def file_path(self, session_id: str, relative_name: str) -> Path:
        """Resolve a listed regular output file without allowing path traversal."""

        output = self.output_directory(session_id)
        path = self._contained(output / relative_name)
        if not path.is_relative_to(output):
            raise ValueError("Path escapes session output directory")
        if relative_name not in self.list_files(session_id):
            raise FileNotFoundError(relative_name)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(relative_name)
        return path

    def _contained(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.data_root):
            raise ValueError("Path escapes data root")
        return resolved

    @staticmethod
    def _has_complete_measurement_row(
        path: Path,
        mode: LutMode,
        request: LightMeasurementRequest,
    ) -> bool:
        if not path.is_file() or path.is_symlink():
            return False
        try:
            raw = path.read_bytes()
            if not raw.endswith((b"\n", b"\r")):
                return False
            rows = list(csv.reader(raw.decode("utf-8").splitlines()))
            if len(rows) < 2 or rows[0] != CSV_HEADERS[mode]:
                return False
            variation = variation_from_csv_row(rows[-1], mode)
            if variation is None:
                return False
            return SessionStorage._variation_matches_request(variation, mode, request)
        except OSError, ValueError:
            return False

    @staticmethod
    def _variation_matches_request(
        variation: Variation,
        mode: LutMode,
        request: LightMeasurementRequest,
    ) -> bool:
        """Check the parsed row against the exact plan the runner would rebuild on resume."""
        from measure.runner.smart_envelope import smart_applies

        if smart_applies(mode, request.parameters):
            # Smart invents rows after the initial sweep. Resume replays the planner
            # from every complete CSV row, so any parseable point of this mode is enough.
            if mode == LutMode.COLOR_TEMP:
                return isinstance(variation, ColorTempVariation)
            if mode == LutMode.HS:
                return isinstance(variation, HsVariation)
            return False
        light_info = LightInfo("unknown", min_mired=MIN_MIRED, max_mired=MAX_MIRED)
        effects = [variation.effect] if isinstance(variation, EffectVariation) else ()
        plan_variations = build_light_plan({mode}, request.parameters, light_info, effects).for_mode(mode).variations
        if isinstance(variation, ColorTempVariation):
            # The light's real mired range is unknown offline, so only the brightness
            # column can be validated against the plan. `MIN_MIRED`/`MAX_MIRED` are a
            # generic placeholder used only because the real range isn't known here --
            # they are not a real device's actual range. Bounding `variation.ct` against
            # them used to reject perfectly good rows from any light whose real range
            # extends past the placeholder (e.g. a warm-white bulb down to ~553 mired,
            # well past the 500 ceiling), which silently turned a resumable failure into
            # an unresumable one. `ct` is still constrained to a positive int by
            # `variation_from_csv_row`, so no bound is needed here at all.
            return variation.bri in {row.bri for row in plan_variations}
        return variation in plan_variations

    def _load_model(self, path: Path, model: type[_ModelT], description: str) -> _ModelT | None:
        """Load one optional JSON document, treating an unreadable or outdated file as absent."""

        if not path.exists():
            return None
        try:
            return model.model_validate(self._read_json(path))
        except (OSError, ValueError) as error:
            _LOGGER.warning("Ignoring invalid %s: %s", description, error)
            return None

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open(encoding="utf-8") as file:
            value = json.load(file)
        if not isinstance(value, dict):
            raise ValueError(f"Expected object in {path}")
        return value

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        write_json_atomic(path, value)
