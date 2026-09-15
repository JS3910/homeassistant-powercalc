import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import fields as dataclass_fields
from dataclasses import replace
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from measure.assembler import MeasurementAssembler
from measure.clock import elapsed_seconds
from measure.const import PARAMETER_LIMITS, MeasureType
from measure.controller.light.const import LutMode
from measure.dummy_load import DummyLoadCalibration, power_meter_fingerprint
from measure.execution import ImmediateInteraction, OperatingPoint
from measure.ha_app.access import is_loopback_address, trusted_ingress_only_enabled
from measure.ha_app.contribution import (
    ConnectPatRequest,
    ContributionApiCoordinator,
    ContributionApiError,
    ContributionApiErrorCode,
    ContributionAuthStatus,
    ContributionPreviewRequest,
    ContributionPreviewResponse,
    ContributionStatus,
    ContributionSubmissionResult,
    ContributionSubmitRequest,
    DeviceFlowPollResponse,
    DeviceFlowStartResponse,
)
from measure.ha_app.coordinator import MeasurementCoordinator, MergeEligibilityError, SessionConflictError
from measure.ha_app.diagnostics import DIAGNOSTIC_EVENT_LIMIT, build_session_diagnostics
from measure.ha_app.library_catalog import (
    DeviceSpecificationCatalog,
    LibraryCatalogError,
    ManufacturerCatalog,
    MeasureDeviceCatalog,
)
from measure.ha_app.light_probe import (
    LightLoadProbe,
    LightLoadProbeError,
    LightLoadProbePoint,
    LightLoadProbeReading,
    LightLoadProbeResult,
    LightLoadProbeStep,
    app_measurement_assembler,
    complete_probe_result,
    light_load_probe_applies,
)
from measure.ha_app.meter_preview import MeterPreviewService
from measure.ha_app.ocr_preview import OcrPreviewRegistry

if TYPE_CHECKING:
    # Importing this for real would require the optional `ocr` extra (it pulls in
    # cv2) just to run the app at all; only ever needed here for type annotations.
    from measure.powermeter.ocr.preview import PreviewServer
from measure.ha_app.preferences import AppPreferences, AppSettingsResponse, AppSettingsUpdate, WitnessMeterSettings
from measure.ha_app.preflight import (
    ActiveSessionError,
    HistoricalRunTiming,
    MeasurementPreflight,
    PreflightError,
    estimate_light_measurement,
)
from measure.ha_app.registry import FieldControl, FieldRole, measurement_definitions
from measure.ha_app.service import MeasurementService
from measure.ha_app.session import (
    ACTIVE_SESSION_STATES,
    PRE_DATA_SESSION_STATES,
    RESUMABLE_SESSION_STATES,
    SessionEvent,
    SessionEventType,
    SessionSnapshot,
    SessionState,
    snapshot_progress_timing,
)
from measure.ha_app.session_display import (
    descriptor_for_request,
    points_this_run,
    raw_since_timestamp,
    session_duration_seconds,
    session_family_key,
    session_identity,
    session_modes,
)
from measure.ha_app.shelly_credentials import ShellyCredentials
from measure.ha_app.shelly_discovery import ShellyDiscoveryResponse, ShellyDiscoveryService
from measure.ha_app.status import MeasureStatusPublisher
from measure.ha_app.storage import SESSION_LOAD_ERRORS, SessionStorage
from measure.home_assistant import HomeAssistantManager
from measure.home_assistant_entities import (
    DeviceClass,
    EntityCatalogSnapshot,
    EntityDescriptor,
    EntityDomain,
    HomeAssistantEntityCatalog,
)
from measure.powermeter.const import PowerMeterType
from measure.powermeter.diagnostics import DiagnosticStatus, PowerMeterDiagnostic, PowerMeterDiagnostics
from measure.powermeter.errors import PowerMeterError
from measure.powermeter.powermeter import PowerMeter
from measure.powermeter.spec import (
    CompositePowerMeterSpec,
    DummyPowerMeterSpec,
    HassPowerMeterSpec,
    KasaPowerMeterSpec,
    MyStromPowerMeterSpec,
    OcrPowerMeterSpec,
    OwonOwh98xxPowerMeterSpec,
    PowerMeterSpec,
    ShellyPowerMeterSpec,
    SinglePowerMeterSpec,
    TasmotaPowerMeterSpec,
    TuyaPowerMeterSpec,
    WitnessSpec,
)
from measure.request import LightMeasurementRequest, MeasurementRequest, ResumePolicy
from measure.runner.errors import RunnerError
from measure.runner.light_plan import ColorTempVariation, EffectVariation, HsVariation, Variation
from measure.runner.lut_csv import load_session_measured_rows, load_session_measured_variations
from measure.runner.plot_edits import PlotEditError, apply_plot_action, load_ignored
from measure.runner.smart_envelope import MeasuredPoint
from measure.runner.sweep_coverage import build_sweep_coverage
from measure.tuning import MeasurementParameters
from measure.version import measure_version
from measure.visualization import PlotSpec, build_session_plots

CACHE_CONTROL_LIBRARY = "public, max-age=600"
_LOGGER = logging.getLogger("measure")


class ErrorResponse(BaseModel):
    code: str
    message: str
    field: str | None = None
    help_url: str | None = None
    help_label: str | None = None


class DocumentedHTTPException(HTTPException):
    """HTTP failure with a structured documentation action for API clients."""

    def __init__(self, *, status_code: int, detail: str, help_url: str, help_label: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.help_url = help_url
        self.help_label = help_label


# Shared OpenAPI documentation for the JSON error body returned by every failed request.
_ERROR = {"model": ErrorResponse}

_CONTRIBUTION_STATUS_CODES = {
    ContributionApiErrorCode.AUTH_UNAVAILABLE: 401,
    ContributionApiErrorCode.SESSION_REQUIRED: 404,
    ContributionApiErrorCode.FLOW_NOT_FOUND: 404,
    ContributionApiErrorCode.SESSION_NOT_READY: 409,
    ContributionApiErrorCode.PREVIEW_REQUIRED: 409,
    ContributionApiErrorCode.CONTRIBUTION_ACTIVE: 409,
    ContributionApiErrorCode.ARTIFACTS_REQUIRED: 422,
    ContributionApiErrorCode.INVALID_METADATA: 422,
    ContributionApiErrorCode.SUBMISSION_FAILED: 502,
}
MeasurementRequestPayload = Annotated[MeasurementRequest, Body(discriminator="measure_type")]


class PreflightResponse(BaseModel):
    valid: bool
    warnings: list[str]
    estimated_variations: int | None = None
    estimated_duration_seconds: int | None = None
    supported_modes: list[LutMode] | None = None
    power_meter_diagnostic: PowerMeterDiagnostic | None = None
    battery_level_entity_id: str | None = None
    battery_level_attribute: str | None = None
    light_load_probe: LightLoadProbeResult | None = None
    probe_steps: list[LightLoadProbeStep] | None = None


class LightLoadProbeCompleteRequest(BaseModel):
    standby_aggregate_power_w: float
    points: list[LightLoadProbePoint]


class EntityCatalogResponse(BaseModel):
    lights: list[EntityDescriptor]
    powers: list[EntityDescriptor]
    voltages: list[EntityDescriptor]


class MeasureDeviceCatalogResponse(BaseModel):
    devices: list[str]


class ManufacturerCatalogResponse(BaseModel):
    manufacturers: list[str]


class DeviceSpecificationFieldResponse(BaseModel):
    name: str
    label: str
    description: str
    value_type: Literal["string", "number", "integer", "boolean"]
    collection: Literal["scalar", "array", "scalar_or_array"]
    options: list[str]


class DeviceSpecificationCatalogResponse(BaseModel):
    device_types: dict[str, list[DeviceSpecificationFieldResponse]]


class SessionFile(BaseModel):
    name: str
    size: int
    media_type: str


class SessionPlots(BaseModel):
    partial: bool
    plots: list[PlotSpec]
    warnings: list[str]
    editable: bool = False


class PlotPointActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_id: str = Field(min_length=1, max_length=200)
    action: Literal["edit", "fix_outlier", "ignore", "unignore", "delete"]
    watt: float | None = Field(default=None, ge=0, le=100_000)


class MergePairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)


class MergeModePreview(BaseModel):
    kept: int
    added: int
    replaced: int
    replaced_reasons: dict[str, int]


class MergePreviewResponse(BaseModel):
    left: str
    right: str
    model_id_warning: bool
    modes: dict[str, MergeModePreview]


class SessionSummary(BaseModel):
    session_id: str
    state: SessionState
    created_at: str
    updated_at: str
    measure_type: MeasureType
    model_id: str
    product_name: str
    manufacturer: str = ""
    measure_device: str
    completed: int
    total: int
    percent: float
    can_resume: bool
    can_refine: bool = False
    can_merge: bool = False
    file_count: int
    size: int
    active: bool
    family_key: str = ""
    modes: list[str] = Field(default_factory=list)
    run_started_at: str | None = None
    duration_seconds: int | None = None
    already_measured: int = 0
    measured: int = 0
    seed_session_id: str | None = None
    can_analyse: bool = False


class SessionProgressResponse(BaseModel):
    completed: int
    total: int
    skipped: int
    already_measured: int = 0
    percent: float
    elapsed_seconds: int | None = None
    estimated_remaining_seconds: int | None


class CalibrationSampleResponse(BaseModel):
    power: float
    resistance: float
    voltage: float


class SessionSnapshotResponse(BaseModel):
    """Complete JSON contract returned by session endpoints and embedded in SSE events."""

    session_id: str
    state: SessionState
    created_at: str
    updated_at: str
    phase: str | None
    activity_reason: str | None = None
    confirmation_message: str | None
    confirmation_action: str | None
    mode: str | None
    run_started_at: str | None = None
    wait_ends_at: str | None = None
    wait_seconds: float | None = None
    progress: SessionProgressResponse
    warnings: list[str]
    error: str | None
    summary: dict[str, str] | None
    operating_point: OperatingPoint | None
    calibration_sample: CalibrationSampleResponse | None
    entity_states: dict[str, str]
    can_analyse: bool
    request: MeasurementRequest
    sweep_coverage: object | None = None


class SessionEventResponse(BaseModel):
    """Wire envelope shared by stored session events and SSE heartbeats."""

    sequence: int
    type: str
    data: dict[str, object]
    snapshot: SessionSnapshotResponse | None = None


class CapabilitiesResponse(BaseModel):
    runtime_version: str
    defaults: dict[str, int | float | bool]
    limits: dict[str, dict[str, int | float]]
    developer_mode: bool = False
    fast_test_mode: bool = False


class LightModeEstimateResponse(BaseModel):
    mode: LutMode
    axes: dict[str, int]
    points: int
    summary: str


class LightEstimateResponse(BaseModel):
    modes: list[LightModeEstimateResponse]
    total_points: int
    total_readings: int | None = None
    max_duration_seconds: int
    used_default_range: bool
    remaining_points: int | None = None
    estimated_duration_seconds: int | None = None
    estimated_from_runs: int | None = None


class FormFieldOption(BaseModel):
    value: str
    label: str
    entity_domain: str | None = None
    enables: list[str] = Field(default_factory=list)
    description: str = ""
    guidance: list[str] = Field(default_factory=list)


class FormField(BaseModel):
    name: str
    label: str
    control: FieldControl
    role: FieldRole = FieldRole.ATTRIBUTE
    narrowed_by: str | None = None
    required: bool = True
    entity_domains: list[str] = Field(default_factory=list)
    options: list[FormFieldOption] = Field(default_factory=list)
    default: str | int | bool | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    step: str | None = None
    multiple: bool = False
    plural_label: str = ""
    derived_from: str | None = None
    hint: str = ""
    visible_when: dict[str, list[str]] = Field(default_factory=dict)
    all_entities: bool = False
    entity_device_classes: list[str] = Field(default_factory=list)
    related_to: str | None = None
    same_device_only: bool = False
    review: bool = False


class MeasureParameter(BaseModel):
    name: str
    label: str
    hint: str = ""
    step: str = "1"
    group: str = ""
    requires_multiple: str | None = None
    control: str = "number"
    bisection: str | None = None
    all_values: str | None = None
    sweep_label: str | None = None
    axis: str | None = None


class MeasureDefinition(BaseModel):
    measure_type: MeasureType
    label: str
    description: str
    icon: str
    confirmation_action: str | None
    confirmation_is_warning: bool = False
    model_id_example: str = ""
    product_name_example: str = ""
    fields: list[FormField]
    parameters: list[MeasureParameter] = Field(default_factory=list)
    supports_profile: bool
    supports_resume: bool


class AppContext:
    def __init__(
        self,
        *,
        data_root: Path,
        hass_url: str,
        hass_token: str,
        trusted_ingress_only: bool,
        developer_mode: bool = False,
    ) -> None:
        self.home_assistant = HomeAssistantManager(hass_url, hass_token)
        self.trusted_ingress_only = trusted_ingress_only
        self.developer_mode = developer_mode
        self.storage = SessionStorage(data_root)
        self.measure_device_catalog = MeasureDeviceCatalog()
        self.manufacturer_catalog = ManufacturerCatalog()
        self.device_specification_catalog = DeviceSpecificationCatalog()
        self.ocr_previews = OcrPreviewRegistry()
        self.meter_previews = MeterPreviewService(self.ocr_previews)
        self.power_meter_diagnostics = PowerMeterDiagnostics(self.build_power_meter)
        self.light_load_probe = LightLoadProbe(
            lambda: app_measurement_assembler(
                home_assistant=self.home_assistant,
                shelly_password=self.shelly_password(),
            ),
            build_power_meter=self.build_power_meter,
        )
        self.contribution = ContributionApiCoordinator(
            self.storage,
            resolve_integration=self.entity_integrations,
            resolve_manufacturer=self.entity_manufacturers,
            resolve_model_id=self.entity_model_ids,
        )
        self.coordinator = MeasurementCoordinator(
            self.storage,
            self._measurement_service,
        )

    def entity_integrations(self, entity_ids: Sequence[str]) -> dict[str, str | None]:
        """Look up which integration provides each entity; contribution details stay usable without it."""
        entities = self._entity_descriptors(entity_ids, "integration")
        return {entity_id: entity.integration if entity is not None else None for entity_id, entity in entities.items()}

    def entity_manufacturers(self, entity_ids: Sequence[str]) -> dict[str, str | None]:
        """Look up HA's device manufacturer per entity and normalize known aliases to the library name."""
        entities = self._entity_descriptors(entity_ids, "manufacturer")
        return {
            entity_id: self._canonical_manufacturer(entity.manufacturer) if entity is not None else None
            for entity_id, entity in entities.items()
        }

    def entity_model_ids(self, entity_ids: Sequence[str]) -> dict[str, str | None]:
        entities = self._entity_descriptors(entity_ids, "model ID")
        return {entity_id: entity.model_id if entity is not None else None for entity_id, entity in entities.items()}

    def _entity_descriptors(self, entity_ids: Sequence[str], purpose: str) -> dict[str, EntityDescriptor | None]:
        """Read one entity snapshot for the whole batch, rather than one per entity."""
        try:
            snapshot = HomeAssistantEntityCatalog(self.home_assistant).load_snapshot()
        except Exception as error:  # noqa: BLE001 - this metadata is optional context for a pull request
            _LOGGER.warning("Could not resolve the %s for %s: %s", purpose, ", ".join(entity_ids), error)
            return dict.fromkeys(entity_ids)
        return {entity_id: snapshot.get(entity_id) for entity_id in entity_ids}

    def _canonical_manufacturer(self, manufacturer: str | None) -> str | None:
        if not manufacturer:
            return None
        try:
            return self.manufacturer_catalog.canonical_name(manufacturer)
        except LibraryCatalogError as error:
            _LOGGER.warning("Could not normalize manufacturer %s: %s", manufacturer, error)
            return manufacturer

    def _measurement_service(self) -> MeasurementService:
        # A real session must own its meter. The setup-page aiming preview binds the
        # same OCR preview port, so it has to be gone before assemble() builds another.
        self.meter_previews.stop_all()
        return MeasurementService(
            self.home_assistant,
            self.storage,
            shelly_password=self.shelly_password(),
            ocr_previews=self.ocr_previews,
        )

    def shelly_password(self) -> str | None:
        credentials = self.storage.load_shelly_credentials()
        return credentials.password if credentials is not None else None

    def build_power_meter(self, spec: PowerMeterSpec, *, shelly_password: str | None = None) -> PowerMeter:
        """Build a meter, reusing a live setup-page OCR preview when it already owns the port.

        Diagnostics and the light-load check each construct their own meter and close
        it afterwards. If the aiming preview is already bound to ``127.0.0.1:8765``,
        a second construct fails instantly with ``Address already in use``. Borrow
        that live meter instead — the wrapper's ``close()`` is a no-op so the preview
        survives the probe.
        """

        borrowed = self.meter_previews.borrow(spec)
        if borrowed is not None:
            return borrowed
        return MeasurementAssembler(
            ImmediateInteraction(),
            home_assistant=self.home_assistant,
            shelly_password=self.shelly_password() if shelly_password is None else shelly_password,
        ).build_power_meter(spec)


_SHUTDOWN_STOP_TIMEOUT_SECONDS = 8.0


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    context = cast(AppContext, app.state.context)
    status_publisher = MeasureStatusPublisher(context.home_assistant, context.coordinator)
    await status_publisher.async_start()
    # Resume in the background so /health can answer. Blocking startup on OCR or
    # Home Assistant being ready is how Supervisor's watchdog SIGKILLs us (137).
    resume_task = asyncio.create_task(_auto_resume_until_running(context))
    try:
        yield
    finally:
        resume_task.cancel()
        with suppress(asyncio.CancelledError):
            await resume_task
        await run_in_threadpool(context.coordinator.request_shutdown_stop, _SHUTDOWN_STOP_TIMEOUT_SECONDS)
        await status_publisher.async_stop()
        context.home_assistant.close()


async def _auto_resume_until_running(context: AppContext) -> None:
    """Retry crash-resume until the session is running or no longer resumable."""

    delay = 2.0
    attempt = 0
    while True:
        snapshot = context.coordinator.current
        if snapshot is None or snapshot.state != SessionState.RESUMABLE:
            return
        try:
            await run_in_threadpool(_auto_resume_interrupted_session, context)
            return
        except Exception:
            attempt += 1
            _LOGGER.warning(
                "Auto-resume attempt %d failed; retrying in %.0fs",
                attempt,
                delay,
                exc_info=attempt == 1,
            )
            await asyncio.sleep(delay)
            delay = min(30.0, 2.0 * (1.5 ** (attempt - 1)))


def _auto_resume_interrupted_session(context: AppContext) -> None:
    """Pick back up a session the app left running when it was last stopped.

    A measurement is meant to run unattended for hours, so a plain process restart --
    an add-on update, a Supervisor cold-backup stopping every add-on before it snapshots
    the filesystem, an OOM kill, anything short of losing the actual persisted output --
    must not need a human to notice and click Resume to keep going.

    `MeasurementCoordinator.__init__` already calls `SessionStorage.load_current()`,
    which promotes a session that was still active when the process last exited to
    RESUMABLE (if its last complete row is still usable) or FAILED (if not). Only
    RESUMABLE is auto-relaunched here: FAILED means there is no compatible resumption
    point to build on, and blindly starting over would repeat whatever made the row
    incompatible rather than actually recovering. `skip_light_load_probe` and
    `skip_power_meter_diagnostic` match `_resume_session` -- the light and meter
    were already validated. Recoverable boot races (OCR has no frame yet, the
    websocket is not up) raise so the background loop can try again.
    """
    snapshot = context.coordinator.current
    if snapshot is None or snapshot.state != SessionState.RESUMABLE:
        return
    request = context.storage.load_request(snapshot.id)
    _preflight(
        context,
        request,
        skip_light_load_probe=True,
        skip_power_meter_diagnostic=True,
    )
    context.coordinator.resume(snapshot.id)
    _LOGGER.info("Automatically resumed session %s after an interrupted run", snapshot.id)


def create_app(
    *,
    data_root: Path,
    hass_url: str = "ws://supervisor/core/websocket",
    hass_token: str | None = None,
    static_root: Path | None = None,
    trusted_ingress_only: bool | None = None,
    developer_mode: bool = False,
) -> FastAPI:
    token = hass_token or os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is required to start the Home Assistant app")
    if trusted_ingress_only is None:
        trusted_ingress_only = trusted_ingress_only_enabled()
    context = AppContext(
        data_root=data_root,
        hass_url=hass_url,
        hass_token=token,
        trusted_ingress_only=trusted_ingress_only,
        developer_mode=developer_mode,
    )
    app = FastAPI(
        title="Powercalc Measure",
        version=measure_version(),
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.context = context
    app.include_router(_router())

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.middleware("http")
    async def restrict_access(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        client_host = request.client.host if request.client else None
        if context.trusted_ingress_only:
            allowed = client_host == "172.30.32.2"
            code, message = "ingress_required", "Ingress access required"
        else:
            allowed = is_loopback_address(client_host)
            code, message = "local_access_required", "Local access required"
        # /health is probed by the container HEALTHCHECK from localhost and
        # exposes no data, so it bypasses the ingress source check.
        if request.url.path != "/health" and not allowed:
            error = ErrorResponse(code=code, message=message)
            return JSONResponse(status_code=403, content=error.model_dump())
        return await call_next(request)

    _register_error_handlers(app)

    assets = static_root or Path(__file__).parent.parent / "static"
    if assets.exists():
        assets_directory = assets / "assets"
        if assets_directory.exists():
            app.mount("/assets", StaticFiles(directory=assets_directory), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(
                assets / "index.html",
                headers={"Cache-Control": "no-store, max-age=0"},
            )

    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        first = error.errors()[0] if error.errors() else {}
        location = first.get("loc", ())
        field = ".".join(str(part) for part in location if part != "body" and part not in MeasureType) or None
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                code="validation_error",
                message=str(first.get("msg", "Invalid request")),
                field=field,
            ).model_dump(),
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, error: HTTPException) -> JSONResponse:
        detail = str(error.detail)
        code = {
            400: "bad_request",
            404: "not_found",
            409: "session_conflict",
            422: "preflight_failed",
        }.get(error.status_code, "request_failed")
        content = ErrorResponse(
            code=code,
            message=detail,
            help_url=getattr(error, "help_url", None),
            help_label=getattr(error, "help_label", None),
        ).model_dump()
        if content["help_url"] is None or content["help_label"] is None:
            content.pop("help_url")
            content.pop("help_label")
        return JSONResponse(
            status_code=error.status_code,
            content=content,
        )

    @app.exception_handler(ContributionApiError)
    async def contribution_error(_: Request, error: ContributionApiError) -> JSONResponse:
        status_code = _CONTRIBUTION_STATUS_CODES.get(error.code, 500)
        return JSONResponse(
            status_code=status_code,
            content=ErrorResponse(code=error.code.value, message=str(error), field=error.field).model_dump(),
        )

    @app.exception_handler(Exception)
    async def internal_error(_: Request, error: Exception) -> JSONResponse:
        # Not lexically inside an `except` block, so `.exception()` trips LOG004.
        # `.error()` with an explicit exc_info logs at the same level with the traceback.
        _LOGGER.error("Unhandled measure app request error", exc_info=error)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(code="internal_error", message="Internal server error").model_dump(),
        )


def _router() -> APIRouter:
    router = APIRouter(prefix="/api")
    _register_measurement_routes(router)
    _register_session_routes(router)
    _register_contribution_routes(router)
    _register_ocr_preview_routes(router)
    _register_meter_preview_routes(router)
    return router


def _register_measurement_routes(router: APIRouter) -> None:  # noqa: C901
    @router.get("/capabilities")
    async def capabilities(request: Request) -> CapabilitiesResponse:
        context = _context(request)
        defaults = MeasurementParameters()
        settings = await run_in_threadpool(context.storage.load_settings)
        numeric = {name: getattr(defaults, name) for name in PARAMETER_LIMITS}
        flags = {
            field.name: getattr(defaults, field.name)
            for field in dataclass_fields(MeasurementParameters)
            if isinstance(getattr(defaults, field.name), bool) and field.name != "fast_test_mode"
        }
        return CapabilitiesResponse(
            runtime_version=measure_version(),
            defaults=numeric | flags | settings.measurement_defaults.model_dump(),
            limits={name: {"min": minimum, "max": maximum} for name, (minimum, maximum) in PARAMETER_LIMITS.items()},
            developer_mode=context.developer_mode,
            fast_test_mode=context.developer_mode and settings.fast_test_mode,
        )

    @router.get("/measure-definitions")
    async def measure_definitions() -> list[MeasureDefinition]:
        return _measure_definitions()

    @router.get("/library/measure-devices", responses={503: _ERROR})
    async def measure_devices(request: Request, response: Response) -> MeasureDeviceCatalogResponse:
        try:
            devices = await run_in_threadpool(_context(request).measure_device_catalog.devices)
        except LibraryCatalogError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        response.headers["Cache-Control"] = CACHE_CONTROL_LIBRARY
        return MeasureDeviceCatalogResponse(devices=list(devices))

    @router.get("/library/manufacturers", responses={503: _ERROR})
    async def manufacturers(request: Request, response: Response) -> ManufacturerCatalogResponse:
        try:
            values = await run_in_threadpool(_context(request).manufacturer_catalog.manufacturers)
        except LibraryCatalogError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        response.headers["Cache-Control"] = CACHE_CONTROL_LIBRARY
        return ManufacturerCatalogResponse(manufacturers=list(values))

    @router.get("/library/device-specifications", responses={503: _ERROR})
    async def device_specifications(request: Request, response: Response) -> DeviceSpecificationCatalogResponse:
        try:
            values = await run_in_threadpool(_context(request).device_specification_catalog.fields)
        except LibraryCatalogError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        response.headers["Cache-Control"] = CACHE_CONTROL_LIBRARY
        return DeviceSpecificationCatalogResponse(
            device_types={
                device_type: [
                    DeviceSpecificationFieldResponse(
                        name=field.name,
                        label=field.label,
                        description=field.description,
                        value_type=field.value_type,
                        collection=field.collection,
                        options=list(field.options),
                    )
                    for field in fields
                ]
                for device_type, fields in values.items()
            },
        )

    @router.get("/settings")
    async def get_settings(request: Request) -> AppSettingsResponse:
        return await run_in_threadpool(_settings_response, _context(request))

    @router.put("/settings", responses={400: _ERROR})
    async def update_settings(payload: AppSettingsUpdate, request: Request) -> AppSettingsResponse:
        context = _context(request)
        if payload.fast_test_mode and not context.developer_mode:
            raise HTTPException(status_code=400, detail="Fast test mode requires developer mode")
        return await run_in_threadpool(_save_settings, context, payload)

    @router.post("/settings/test-power-meter")
    async def test_power_meter(payload: AppSettingsUpdate, request: Request) -> PowerMeterDiagnostic:
        return await run_in_threadpool(_test_power_meter, _context(request), payload)

    @router.get("/power-meters/shelly")
    async def discover_shelly_power_meters(request: Request) -> ShellyDiscoveryResponse:
        return await ShellyDiscoveryService(_context(request).home_assistant).discover()

    @router.get("/dummy-load/calibration")
    async def dummy_load_calibration(request: Request) -> DummyLoadCalibration | None:
        return await run_in_threadpool(_matching_dummy_load_calibration, _context(request))

    @router.get("/entity-catalog")
    async def entity_catalog(request: Request) -> EntityCatalogResponse:
        snapshot = await run_in_threadpool(
            HomeAssistantEntityCatalog(_context(request).home_assistant).load_snapshot,
        )
        return EntityCatalogResponse(
            lights=snapshot.select(domain=EntityDomain.LIGHT),
            powers=snapshot.select(device_class=DeviceClass.POWER),
            voltages=snapshot.select(device_class=DeviceClass.VOLTAGE),
        )

    @router.get("/entities", responses={400: _ERROR})
    async def entities(
        request: Request,
        domain: Annotated[EntityDomain | None, Query()] = None,
        device_class: Annotated[DeviceClass | None, Query()] = None,
        all_entities: Annotated[bool, Query(alias="all")] = False,
    ) -> list[EntityDescriptor]:
        if sum((domain is not None, device_class is not None, all_entities)) != 1:
            raise HTTPException(status_code=400, detail="Specify exactly one entity filter")
        snapshot = await run_in_threadpool(
            HomeAssistantEntityCatalog(_context(request).home_assistant).load_snapshot,
        )
        return snapshot.all() if all_entities else snapshot.select(domain=domain, device_class=device_class)

    @router.post("/preflight", responses={409: _ERROR, 422: _ERROR})
    async def preflight(payload: MeasurementRequestPayload, request: Request) -> PreflightResponse:
        context = _context(request)
        prepared = await run_in_threadpool(_apply_fast_test_mode, context, payload)
        return await run_in_threadpool(_preflight, context, prepared)

    @router.post("/preflight/probe", responses={422: _ERROR})
    async def preflight_probe(
        payload: MeasurementRequestPayload,
        request: Request,
        step: Annotated[str, Query()],
    ) -> LightLoadProbeReading:
        if not isinstance(payload, LightMeasurementRequest):
            raise HTTPException(status_code=422, detail="The light check is only available for light measurements")
        context = _context(request)
        prepared = await run_in_threadpool(_apply_fast_test_mode, context, payload)
        if not isinstance(prepared, LightMeasurementRequest):
            raise HTTPException(status_code=422, detail="The light check is only available for light measurements")
        try:
            return await run_in_threadpool(context.light_load_probe.measure_step, prepared, step)
        except LightLoadProbeError as error:
            raise _probe_http_error(error) from error

    @router.post("/preflight/probe/complete", responses={422: _ERROR})
    async def preflight_probe_complete(payload: LightLoadProbeCompleteRequest) -> LightLoadProbeResult:
        try:
            return complete_probe_result(payload.standby_aggregate_power_w, payload.points)
        except LightLoadProbeError as error:
            raise _probe_http_error(error) from error

    @router.post("/estimate", responses={422: _ERROR})
    async def estimate(payload: MeasurementRequestPayload, request: Request) -> LightEstimateResponse:
        if not isinstance(payload, LightMeasurementRequest):
            raise HTTPException(status_code=422, detail="Estimate is only available for light measurements")
        context = _context(request)
        prepared = await run_in_threadpool(_apply_fast_test_mode, context, payload)
        return await run_in_threadpool(_estimate, context, prepared)


def _register_session_routes(router: APIRouter) -> None:  # noqa: C901
    @router.post("/sessions/merge/preview", responses={404: _ERROR, 409: _ERROR, 422: _ERROR})
    async def merge_preview(payload: MergePairRequest, request: Request) -> MergePreviewResponse:
        context = _context(request)
        try:
            preview = await run_in_threadpool(context.coordinator.preview_merge, payload.left, payload.right)
        except MergeEligibilityError as error:
            status = 404 if "does not exist" in str(error) else 422
            raise HTTPException(status_code=status, detail=str(error)) from error
        except SessionConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SESSION_LOAD_ERRORS as error:
            raise HTTPException(status_code=404, detail="The requested session does not exist") from error
        return MergePreviewResponse.model_validate(preview)

    @router.post("/sessions/merge", status_code=201, responses={404: _ERROR, 409: _ERROR, 422: _ERROR})
    async def merge_sessions(payload: MergePairRequest, request: Request) -> SessionSnapshotResponse:
        context = _context(request)
        try:
            snapshot = await run_in_threadpool(context.coordinator.merge, payload.left, payload.right)
        except MergeEligibilityError as error:
            status = 404 if "does not exist" in str(error) else 422
            raise HTTPException(status_code=status, detail=str(error)) from error
        except SessionConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SESSION_LOAD_ERRORS as error:
            raise HTTPException(status_code=404, detail="The requested session does not exist") from error
        return _snapshot_response(context, snapshot)

    @router.post("/sessions", status_code=201, responses={409: _ERROR, 422: _ERROR})
    async def start_session(payload: MeasurementRequestPayload, request: Request) -> SessionSnapshotResponse:
        context = _context(request)
        prepared = await run_in_threadpool(_apply_fast_test_mode, context, payload)
        await run_in_threadpool(_preflight, context, prepared, skip_light_load_probe=True)
        try:
            snapshot = context.coordinator.start(prepared)
        except SessionConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _snapshot_response(context, snapshot)

    @router.get("/sessions")
    async def sessions(request: Request) -> list[SessionSummary]:
        return await run_in_threadpool(_session_summaries, _context(request))

    @router.get("/sessions/{session_id}", responses={404: _ERROR})
    async def session(session_id: str, request: Request) -> SessionSnapshotResponse:
        context = _context(request)
        return _snapshot_response(context, _require_session(context, session_id))

    @router.delete("/sessions/{session_id}", status_code=204, responses={404: _ERROR, 409: _ERROR})
    async def delete_session(session_id: str, request: Request) -> Response:
        context = _context(request)
        _require_session(context, session_id)
        try:
            await run_in_threadpool(context.coordinator.delete, session_id)
        except SessionConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=204)

    @router.post("/sessions/{session_id}/cancel", status_code=202, responses={404: _ERROR, 409: _ERROR})
    async def cancel_session(session_id: str, request: Request) -> SessionSnapshotResponse:
        return _cancel_session(_context(request), session_id)

    @router.post("/sessions/{session_id}/confirm", responses={404: _ERROR, 409: _ERROR})
    async def confirm_session(session_id: str, request: Request) -> SessionSnapshotResponse:
        return _confirm_session(_context(request), session_id)

    @router.post("/sessions/{session_id}/resume", responses={404: _ERROR, 409: _ERROR, 422: _ERROR})
    async def resume_session(session_id: str, request: Request) -> SessionSnapshotResponse:
        return await _resume_session(_context(request), session_id)

    @router.post("/sessions/{session_id}/analyse", responses={404: _ERROR, 409: _ERROR})
    async def analyse_session(session_id: str, request: Request) -> SessionSnapshotResponse:
        context = _context(request)
        _require_session(context, session_id)
        try:
            snapshot = await run_in_threadpool(context.coordinator.analyse, session_id)
        except SessionConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _snapshot_response(context, snapshot)

    @router.get("/sessions/{session_id}/files", responses={404: _ERROR})
    async def session_files(session_id: str, request: Request) -> list[SessionFile]:
        context = _context(request)
        snapshot = _require_session(context, session_id)
        return _session_files(context, snapshot)

    @router.get("/sessions/{session_id}/plots", responses={404: _ERROR, 409: _ERROR})
    async def session_plots(session_id: str, request: Request) -> SessionPlots:
        context = _context(request)
        return await _session_plots(context, _require_session(context, session_id))

    @router.post("/sessions/{session_id}/plots/points", responses={404: _ERROR, 409: _ERROR, 422: _ERROR})
    async def edit_plot_point(session_id: str, request: Request, body: PlotPointActionRequest) -> SessionPlots:
        context = _context(request)
        snapshot = _require_session(context, session_id)
        if snapshot.state in ACTIVE_SESSION_STATES:
            raise HTTPException(status_code=409, detail="Wait until the measurement has stopped before editing points")
        try:
            apply_plot_action(
                context.storage.session_directory(snapshot.id),
                context.storage.load_request(snapshot.id),
                body.point_id,
                body.action,
                watt=body.watt,
            )
        except (PlotEditError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return await _session_plots(context, snapshot)

    @router.get("/sessions/{session_id}/files/{name:path}", responses={404: _ERROR})
    async def session_download(session_id: str, name: str, request: Request) -> FileResponse:
        context = _context(request)
        return _session_download(context, _require_session(context, session_id), name)

    @router.get("/sessions/{session_id}/diagnostics", responses={404: _ERROR})
    async def session_diagnostics(session_id: str, request: Request) -> Response:
        context = _context(request)
        return _session_diagnostics(context, _require_session(context, session_id))

    @router.get("/sessions/{session_id}/logs", responses={404: _ERROR})
    async def session_logs(
        session_id: str,
        request: Request,
        after: int = 0,
    ) -> list[dict[str, str | int]]:
        context = _context(request)
        _require_session(context, session_id)
        return _session_log_entries(context, session_id, after=after)

    @router.get("/sessions/{session_id}/events", responses={404: _ERROR})
    async def session_events(session_id: str, request: Request) -> StreamingResponse:
        context = _context(request)
        _require_session(context, session_id)
        return StreamingResponse(
            _event_stream(request, context, session_id),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )


def _register_ocr_preview_routes(router: APIRouter) -> None:
    """In-process access to a running session's OCR camera preview(s), by role label.

    Mirrors the shape `PreviewServer`'s own standalone page already serves
    (`state.json`/`frame.jpg`/`events`), just reached through the app's own ingress-safe
    API instead of a second socket, since the meter and this API run in one process.
    """

    @router.get("/sessions/{session_id}/ocr", responses={404: _ERROR})
    async def ocr_previews(session_id: str, request: Request) -> list[str]:
        context = _context(request)
        _require_session(context, session_id)
        return list(context.ocr_previews.labels(session_id))

    @router.get("/sessions/{session_id}/ocr/{label}/state", responses={404: _ERROR})
    async def ocr_state(session_id: str, label: str, request: Request) -> dict[str, object]:
        context = _context(request)
        preview = _require_ocr_preview(context, session_id, label)
        return preview.state()

    @router.get("/sessions/{session_id}/ocr/{label}/frame.jpg", responses={404: _ERROR, 503: _ERROR})
    async def ocr_frame(session_id: str, label: str, request: Request) -> Response:
        context = _context(request)
        preview = _require_ocr_preview(context, session_id, label)
        jpeg = preview.jpeg()
        if jpeg is None:
            raise HTTPException(status_code=503, detail="No camera frame yet")
        return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @router.get("/sessions/{session_id}/ocr/{label}/events", responses={404: _ERROR})
    async def ocr_events(session_id: str, label: str, request: Request) -> StreamingResponse:
        context = _context(request)
        preview = _require_ocr_preview(context, session_id, label)
        return StreamingResponse(
            _ocr_event_stream(request, preview),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )


def _require_ocr_preview(context: AppContext, session_id: str, label: str) -> "PreviewServer":  # noqa: UP037
    _require_session(context, session_id)
    preview = context.ocr_previews.get(session_id, label)
    if preview is None:
        raise HTTPException(status_code=404, detail="No OCR camera preview for that session/label")
    return preview


class MeterPreviewStartResponse(BaseModel):
    """Nothing to preview (`preview_id` unset) is a normal, successful outcome -- the
    configured meter simply doesn't include an OCR camera."""

    model_config = ConfigDict(frozen=True)

    preview_id: str | None = None
    labels: list[str] = Field(default_factory=list)


def _register_meter_preview_routes(router: APIRouter) -> None:
    """Lets the setup/settings UI show a live OCR camera preview -- so the camera can
    actually be pointed at the meter -- before any real measurement session exists.
    Same underlying mechanism as `_register_ocr_preview_routes`, just keyed by a
    throwaway id instead of a session id; see `MeterPreviewService` for the lifecycle.
    """

    @router.post("/power-meters/ocr-preview")
    async def start_meter_preview(payload: AppSettingsUpdate, request: Request) -> MeterPreviewStartResponse:
        context = _context(request)
        preview_id, labels = await run_in_threadpool(_start_meter_preview, context, payload)
        return MeterPreviewStartResponse(preview_id=preview_id, labels=list(labels))

    @router.delete("/power-meters/ocr-preview/{preview_id}", status_code=204)
    async def stop_meter_preview(preview_id: str, request: Request) -> Response:
        await run_in_threadpool(_context(request).meter_previews.stop, preview_id)
        return Response(status_code=204)

    @router.get("/power-meters/ocr-preview/{preview_id}/{label}/state", responses={404: _ERROR})
    async def meter_preview_state(preview_id: str, label: str, request: Request) -> dict[str, object]:
        preview = _require_meter_preview(_context(request), preview_id, label)
        return preview.state()

    @router.get("/power-meters/ocr-preview/{preview_id}/{label}/frame.jpg", responses={404: _ERROR, 503: _ERROR})
    async def meter_preview_frame(preview_id: str, label: str, request: Request) -> Response:
        preview = _require_meter_preview(_context(request), preview_id, label)
        jpeg = preview.jpeg()
        if jpeg is None:
            raise HTTPException(status_code=503, detail="No camera frame yet")
        return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @router.get("/power-meters/ocr-preview/{preview_id}/{label}/events", responses={404: _ERROR})
    async def meter_preview_events(preview_id: str, label: str, request: Request) -> StreamingResponse:
        context = _context(request)
        preview = _require_meter_preview(context, preview_id, label)
        return StreamingResponse(
            _ocr_event_stream(request, preview, on_tick=lambda: context.meter_previews.touch(preview_id)),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )


def _start_meter_preview(context: AppContext, settings: AppSettingsUpdate) -> tuple[str | None, tuple[str, ...]]:
    try:
        spec = _power_meter_spec(settings.preferences())
    except PowerMeterError:
        # Not addressed enough to build yet (e.g. the IP field is still empty) -- that's
        # the normal state while the form is being filled in, not a failure to report.
        return None, ()
    password = None if settings.clear_shelly_password else settings.shelly_password or context.shelly_password()

    def build(power_meter_spec: PowerMeterSpec) -> PowerMeter:
        return MeasurementAssembler(
            ImmediateInteraction(),
            home_assistant=context.home_assistant,
            shelly_password=password,
        ).build_power_meter(power_meter_spec)

    return context.meter_previews.start(spec, build)


def _require_meter_preview(context: AppContext, preview_id: str, label: str) -> "PreviewServer":  # noqa: UP037
    context.meter_previews.touch(preview_id)
    preview = context.ocr_previews.get(preview_id, label)
    if preview is None:
        raise HTTPException(status_code=404, detail="No camera preview for that id/label")
    return preview


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _ocr_event_stream(
    request: Request,
    preview: "PreviewServer",  # noqa: UP037
    on_tick: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """One SSE message per published frame; a comment keeps an idle connection alive.

    ``on_tick`` runs on every wait (frame or keep-alive) so a long-lived aiming preview
    is not reaped after 60s just because the stream was one HTTP request.
    """

    version = 0  # a client connecting after frames were published gets the current one at once
    while not await request.is_disconnected():
        if preview.closed:
            return
        if on_tick is not None:
            on_tick()
        update = await run_in_threadpool(preview.wait_for_update, version, 1.0)
        if update is None:
            yield ": keep-alive\n\n"
            continue
        version, state = update
        yield f"data: {json.dumps({'version': version, 'state': state})}\n\n"


def _register_contribution_routes(router: APIRouter) -> None:  # noqa: C901
    @router.get("/contribution/auth")
    async def contribution_auth_status(request: Request) -> ContributionAuthStatus:
        return await run_in_threadpool(_context(request).contribution.auth_status)

    @router.put("/contribution/auth")
    async def contribution_connect_pat(payload: ConnectPatRequest, request: Request) -> ContributionAuthStatus:
        return await run_in_threadpool(_context(request).contribution.connect_pat, payload.token)

    @router.delete("/contribution/auth")
    async def contribution_disconnect(request: Request) -> ContributionAuthStatus:
        return await run_in_threadpool(_context(request).contribution.disconnect)

    @router.post("/contribution/auth/device", responses={401: _ERROR})
    async def contribution_device_start(request: Request) -> DeviceFlowStartResponse:
        return await run_in_threadpool(_context(request).contribution.start_device_flow)

    @router.post("/contribution/auth/device/{flow_id}", responses={401: _ERROR, 404: _ERROR})
    async def contribution_device_poll(flow_id: str, request: Request) -> DeviceFlowPollResponse:
        return await run_in_threadpool(_context(request).contribution.poll_device_flow, flow_id)

    @router.get("/contribution/status")
    async def contribution_status(request: Request) -> ContributionStatus:
        return _context(request).contribution.status()

    @router.get("/sessions/{session_id}/contribution", responses={404: _ERROR, 409: _ERROR})
    async def contribution_draft(session_id: str, request: Request) -> ContributionPreviewResponse:
        context = _context(request)
        return await run_in_threadpool(context.contribution.draft, _require_session(context, session_id))

    @router.post(
        "/sessions/{session_id}/contribution/preview",
        responses={404: _ERROR, 409: _ERROR, 422: _ERROR, 502: _ERROR},
    )
    async def contribution_preview(
        session_id: str,
        payload: ContributionPreviewRequest,
        request: Request,
    ) -> ContributionPreviewResponse:
        context = _context(request)
        return await run_in_threadpool(
            context.contribution.preview,
            _require_session(context, session_id),
            payload,
        )

    @router.post(
        "/sessions/{session_id}/contribution",
        responses={401: _ERROR, 404: _ERROR, 409: _ERROR, 502: _ERROR},
    )
    async def contribution_submit(
        session_id: str,
        payload: ContributionSubmitRequest,
        request: Request,
    ) -> ContributionSubmissionResult:
        context = _context(request)
        return await run_in_threadpool(
            context.contribution.submit,
            _require_session(context, session_id),
            payload,
        )

    @router.get(
        "/sessions/{session_id}/contribution/{job_id}/profile.zip",
        responses={404: _ERROR, 409: _ERROR},
    )
    async def contribution_profile_archive(session_id: str, job_id: str, request: Request) -> Response:
        context = _context(request)
        content = await run_in_threadpool(
            context.contribution.prepared_archive,
            _require_session(context, session_id),
            job_id,
        )
        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="powercalc-profile.zip"'},
        )


def _context(request: Request) -> AppContext:
    return cast(AppContext, request.app.state.context)


def _require_session(context: AppContext, session_id: str) -> SessionSnapshot:
    try:
        return context.coordinator.get(session_id)
    except SESSION_LOAD_ERRORS as error:
        raise HTTPException(status_code=404, detail="Measurement session not found") from error


def _cancel_session(context: AppContext, session_id: str) -> SessionSnapshotResponse:
    try:
        snapshot = context.coordinator.cancel(session_id)
    except SessionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _snapshot_response(context, snapshot)


def _confirm_session(context: AppContext, session_id: str) -> SessionSnapshotResponse:
    try:
        snapshot = context.coordinator.confirm(session_id)
    except SessionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _snapshot_response(context, snapshot)


async def _resume_session(context: AppContext, session_id: str) -> SessionSnapshotResponse:
    snapshot = _require_session(context, session_id)
    # Resuming continues a session whose light/power-meter setup was already validated
    # (and, for real lights, already load-probed at full brightness) the first time it
    # ran. Re-running that probe here repeats a slow, disruptive step for no new
    # information, and previously ran long enough to blow past the ingress proxy's
    # response timeout -- the browser gives up and shows nothing, even though the probe
    # (and then the resumed session) keeps running to completion server-side regardless.
    await run_in_threadpool(
        _preflight,
        context,
        context.storage.load_request(snapshot.id),
        skip_light_load_probe=True,
        skip_power_meter_diagnostic=True,
    )
    try:
        snapshot = context.coordinator.resume(snapshot.id)
    except SessionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _snapshot_response(context, snapshot)


def _session_files(context: AppContext, snapshot: SessionSnapshot) -> list[SessionFile]:
    return [
        _file_descriptor(context.storage.file_path(snapshot.id, name), name)
        for name in context.storage.list_files(snapshot.id)
    ]


async def _session_plots(context: AppContext, snapshot: SessionSnapshot) -> SessionPlots:
    if snapshot.state in PRE_DATA_SESSION_STATES:
        raise HTTPException(status_code=409, detail="Plots are available once the measurement starts taking readings")
    names = context.storage.list_files(snapshot.id)
    paths = {name: context.storage.file_path(snapshot.id, name) for name in names}
    result = await run_in_threadpool(
        build_session_plots,
        context.storage.load_request(snapshot.id),
        paths,
        inherited_keys=_inherited_plot_keys(context, snapshot),
        ignored_ids=load_ignored(context.storage.session_directory(snapshot.id)),
        measured_modes_only=snapshot.state in {SessionState.RUNNING, SessionState.CANCELLING},
    )
    return SessionPlots(
        partial=snapshot.state is not SessionState.COMPLETED,
        plots=list(result.plots),
        warnings=list(result.warnings),
        editable=snapshot.state not in ACTIVE_SESSION_STATES,
    )


def _session_download(context: AppContext, snapshot: SessionSnapshot, name: str) -> FileResponse:
    try:
        path = context.storage.file_path(snapshot.id, name)
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=404, detail="File not found") from error
    return FileResponse(path, filename=path.name)


def _session_diagnostics(context: AppContext, snapshot: SessionSnapshot) -> Response:
    files = [
        _file_descriptor(context.storage.file_path(snapshot.id, name), name).model_dump()
        for name in context.storage.list_files(snapshot.id)
    ]
    events = context.storage.load_events(snapshot.id, limit=DIAGNOSTIC_EVENT_LIMIT + 1)
    events_truncated = len(events) > DIAGNOSTIC_EVENT_LIMIT
    payload = build_session_diagnostics(
        snapshot,
        context.storage.load_request(snapshot.id),
        events[-DIAGNOSTIC_EVENT_LIMIT:],
        files,
        events_truncated=events_truncated,
    )
    filename = f"powercalc-measure-diagnostics-{snapshot.id[:8]}.json"
    return Response(
        content=json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _measure_definitions() -> list[MeasureDefinition]:
    return [
        MeasureDefinition(
            measure_type=definition.measure_type,
            label=definition.label,
            description=definition.description,
            icon=definition.icon,
            confirmation_action=definition.confirmation_action,
            confirmation_is_warning=definition.confirmation_is_warning,
            model_id_example=definition.model_id_example,
            product_name_example=definition.product_name_example,
            parameters=[MeasureParameter(**vars(parameter)) for parameter in definition.parameters],
            fields=[
                FormField(
                    name=field.name,
                    label=field.label,
                    control=field.control,
                    role=field.role,
                    narrowed_by=field.narrowed_by,
                    required=field.required,
                    entity_domains=list(field.entity_domains),
                    options=[
                        FormFieldOption(
                            value=option.value,
                            label=option.label,
                            entity_domain=option.entity_domain,
                            enables=list(option.enables),
                            description=option.description,
                            guidance=list(option.guidance),
                        )
                        for option in field.options
                    ],
                    default=field.default,
                    minimum=field.minimum,
                    maximum=field.maximum,
                    step=field.step,
                    multiple=field.multiple,
                    plural_label=field.plural_label,
                    derived_from=field.derived_from,
                    hint=field.hint,
                    visible_when={name: list(values) for name, values in field.visible_when},
                    all_entities=field.all_entities,
                    entity_device_classes=list(field.entity_device_classes),
                    related_to=field.related_to,
                    same_device_only=field.same_device_only,
                    review=field.review,
                )
                for field in definition.fields
            ],
            supports_profile=definition.supports_profile,
            supports_resume=definition.supports_resume,
        )
        for definition in measurement_definitions()
    ]


def _settings_response(context: AppContext) -> AppSettingsResponse:
    settings = context.storage.load_settings()
    return AppSettingsResponse.model_validate(
        settings.model_dump() | {"shelly_password_configured": context.shelly_password() is not None},
    )


def _save_settings(context: AppContext, update: AppSettingsUpdate) -> AppSettingsResponse:
    if update.clear_shelly_password:
        context.storage.clear_shelly_credentials()
    elif update.shelly_password:
        context.storage.save_shelly_credentials(ShellyCredentials(password=update.shelly_password))
    context.storage.save_settings(update.preferences())
    return _settings_response(context)


def _test_power_meter(context: AppContext, settings: AppSettingsUpdate) -> PowerMeterDiagnostic:
    """Validate connectivity and measurement quality for the configured meter."""
    try:
        spec = _power_meter_spec(settings.preferences())
    except PowerMeterError as error:
        message = str(error)
        return PowerMeterDiagnostic(
            success=False,
            status=DiagnosticStatus.POOR,
            precision_status=DiagnosticStatus.UNSUPPORTED,
            update_interval_status=DiagnosticStatus.UNSUPPORTED,
            messages=[message],
            message=message,
        )
    password = None if settings.clear_shelly_password else settings.shelly_password or context.shelly_password()
    return context.power_meter_diagnostics.evaluate(
        spec,
        force=True,
        build_power_meter=lambda power_meter_spec: context.build_power_meter(
            power_meter_spec,
            shelly_password=password,
        ),
    )


def _dummy_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    return DummyPowerMeterSpec()


def _shelly_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    if not draft.device_ip:
        raise PowerMeterError("Enter the Shelly IP address first")
    return ShellyPowerMeterSpec(device_ip=draft.device_ip, username=draft.username)


def _kasa_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    if not draft.device_ip:
        raise PowerMeterError("Enter the Kasa IP address first")
    return KasaPowerMeterSpec(device_ip=draft.device_ip)


def _mystrom_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    if not draft.device_ip:
        raise PowerMeterError("Enter the myStrom IP address first")
    return MyStromPowerMeterSpec(device_ip=draft.device_ip)


def _tasmota_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    if not draft.device_ip:
        raise PowerMeterError("Enter the Tasmota IP address first")
    return TasmotaPowerMeterSpec(device_ip=draft.device_ip)


def _tuya_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    if not draft.device_id or not draft.device_ip:
        raise PowerMeterError("Enter the Tuya device ID and IP address first")
    return TuyaPowerMeterSpec(device_id=draft.device_id, device_ip=draft.device_ip, version=draft.version)


def _owon_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    if not draft.port or draft.baudrate is None or draft.channel is None:
        raise PowerMeterError("Configure the Owon serial port, baud rate, and channel first")
    return OwonOwh98xxPowerMeterSpec(
        port=draft.port,
        baudrate=draft.baudrate,
        timeout=draft.timeout,
        channel=draft.channel,
    )


def _ocr_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    return OcrPowerMeterSpec(
        source=draft.source,
        layout=draft.layout,
        preview_host=draft.preview_host,
        preview_port=draft.preview_port,
        window_seconds=draft.window_seconds,
        stale_after_seconds=draft.stale_after_seconds,
        crosscheck_tolerance_pct=draft.crosscheck_tolerance_pct,
        min_current_for_crosscheck=draft.min_current_for_crosscheck,
    )


def _hass_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    if not draft.entity_id:
        raise PowerMeterError("Select a power sensor first")
    return HassPowerMeterSpec(
        entity_id=draft.entity_id,
        voltage_entity_id=draft.voltage_entity_id,
        max_age_seconds=draft.max_age_seconds,
    )


_SINGLE_METER_SPEC_BUILDERS: dict[PowerMeterType, Callable[[WitnessMeterSettings], SinglePowerMeterSpec]] = {
    PowerMeterType.DUMMY: _dummy_meter_spec,
    PowerMeterType.SHELLY: _shelly_meter_spec,
    PowerMeterType.KASA: _kasa_meter_spec,
    PowerMeterType.MYSTROM: _mystrom_meter_spec,
    PowerMeterType.TASMOTA: _tasmota_meter_spec,
    PowerMeterType.TUYA: _tuya_meter_spec,
    PowerMeterType.OWON_OWH98XX: _owon_meter_spec,
    PowerMeterType.OCR: _ocr_meter_spec,
    PowerMeterType.HASS: _hass_meter_spec,
}
"""Every meter the app can build from a settings-level draft. Deliberately excludes
``MANUAL`` (blocks on ``input()``, which has no console to read from inside the app's
background worker) and ``COMPOSITE`` (a composite wraps these, rather than being one)."""


def _single_meter_spec(draft: WitnessMeterSettings) -> SinglePowerMeterSpec:
    """Convert one settings-level meter draft (the primary's, or a witness's) into a
    validated spec, raising ``PowerMeterError`` naming whatever is still missing."""
    builder = _SINGLE_METER_SPEC_BUILDERS.get(draft.type, _hass_meter_spec)
    return builder(draft)


def _power_meter_spec(settings: AppPreferences) -> PowerMeterSpec:
    """The configured session-default meter: the primary alone, or wrapped as a composite
    with its witnesses once at least one is configured (matching
    ``CompositePowerMeterSpec.witnesses``' minimum length of one)."""
    primary = _single_meter_spec(settings.primary_meter_draft())
    if not settings.witnesses:
        return primary
    return CompositePowerMeterSpec(
        primary=primary,
        witnesses=[
            WitnessSpec(
                meter=_single_meter_spec(witness.meter),
                position=witness.position,
                offset_w=witness.offset_w,
                tolerance_w=witness.tolerance_w,
                tolerance_pct=witness.tolerance_pct,
                required=witness.required,
            )
            for witness in settings.witnesses
        ],
    )


def _matching_dummy_load_calibration(context: AppContext) -> DummyLoadCalibration | None:
    calibration = context.storage.load_dummy_load_calibration()
    if calibration is None:
        return None
    try:
        spec = _power_meter_spec(context.storage.load_settings())
    except PowerMeterError:
        return None
    spec = _with_related_voltage(spec, context)
    return calibration if calibration.power_meter_fingerprint == power_meter_fingerprint(spec) else None


def _with_related_voltage(spec: PowerMeterSpec, context: AppContext) -> PowerMeterSpec:
    """Attach the Home Assistant-associated voltage sensor to a Hass meter, whether it is
    the whole spec or a composite's primary — the fingerprint must reflect it either way
    since it changes what the meter actually reads."""
    if isinstance(spec, HassPowerMeterSpec):
        snapshot = HomeAssistantEntityCatalog(context.home_assistant).load_snapshot()
        return spec.model_copy(
            update={"voltage_entity_id": snapshot.related_entity_id(spec.entity_id, DeviceClass.VOLTAGE)},
        )
    if isinstance(spec, CompositePowerMeterSpec) and isinstance(spec.primary, HassPowerMeterSpec):
        primary = cast(HassPowerMeterSpec, _with_related_voltage(spec.primary, context))
        return spec.model_copy(update={"primary": primary})
    return spec


def _preflight(
    context: AppContext,
    payload: MeasurementRequest,
    *,
    skip_light_load_probe: bool = True,
    include_probe_plan: bool = False,
    skip_power_meter_diagnostic: bool = False,
) -> PreflightResponse:
    catalog = HomeAssistantEntityCatalog(context.home_assistant)
    snapshot = None

    def load_entities(
        domain: EntityDomain | None,
        device_class: DeviceClass | None,
    ) -> list[EntityDescriptor]:
        nonlocal snapshot
        if snapshot is None:
            snapshot = catalog.load_snapshot()
        return snapshot.select(domain=domain, device_class=device_class)

    try:
        result = MeasurementPreflight(
            has_active_session=lambda: _is_active(context.coordinator.current),
            verify_storage=context.storage.verify_writable,
            load_entities=load_entities,
            load_all_entities=lambda: catalog.load_snapshot().all(),
            diagnose_power_meter=context.power_meter_diagnostics.evaluate,
            developer_mode=context.developer_mode,
            load_measured_variations=lambda session_id: _seed_measured_variations(context, session_id),
        ).validate(payload, skip_power_meter_diagnostic=skip_power_meter_diagnostic)
        del skip_light_load_probe
        probe_steps = (
            list(context.light_load_probe.plan(payload))
            if include_probe_plan and light_load_probe_applies(payload)
            else None
        )
    except ActiveSessionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except LightLoadProbeError as error:
        raise _probe_http_error(error) from error
    except PreflightError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return PreflightResponse(
        valid=True,
        warnings=list(result.warnings),
        estimated_variations=result.estimated_variations,
        estimated_duration_seconds=result.estimated_duration_seconds,
        supported_modes=list(result.supported_modes) if result.supported_modes is not None else None,
        power_meter_diagnostic=result.power_meter_diagnostic,
        battery_level_entity_id=result.battery_level_entity_id,
        battery_level_attribute=result.battery_level_attribute,
        light_load_probe=None,
        probe_steps=probe_steps,
    )


def _probe_http_error(error: LightLoadProbeError) -> HTTPException:
    if error.help_url is None or error.help_label is None:
        return HTTPException(status_code=422, detail=str(error))
    return DocumentedHTTPException(
        status_code=422,
        detail=str(error),
        help_url=error.help_url,
        help_label=error.help_label,
    )


def _estimate(context: AppContext, payload: LightMeasurementRequest) -> LightEstimateResponse:
    catalog = HomeAssistantEntityCatalog(context.home_assistant)
    snapshot = None

    def load_entities(
        domain: EntityDomain | None,
        device_class: DeviceClass | None,
    ) -> list[EntityDescriptor]:
        nonlocal snapshot
        if snapshot is None:
            snapshot = catalog.load_snapshot()
        return snapshot.select(domain=domain, device_class=device_class)

    try:
        result = estimate_light_measurement(
            payload,
            load_entities=load_entities,
            load_measured_variations=lambda session_id: _seed_measured_variations(context, session_id),
            load_measured_points=lambda session_id: _seed_measured_points(context, session_id),
            load_historical_runs=lambda: _historical_run_timings(context),
        )
    except RunnerError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return LightEstimateResponse(
        modes=[
            LightModeEstimateResponse(mode=item.mode, axes=item.axes, points=item.points, summary=item.summary)
            for item in result.modes
        ],
        total_points=result.total_points,
        total_readings=result.total_readings,
        max_duration_seconds=result.max_duration_seconds,
        used_default_range=result.used_default_range,
        remaining_points=result.remaining_points,
        estimated_duration_seconds=result.estimated_duration_seconds,
        estimated_from_runs=result.estimated_from_runs,
    )


def _historical_run_timings(context: AppContext) -> tuple[HistoricalRunTiming, ...]:
    """Completed light runs with a usable wall-clock rate, newest-first from storage."""

    runs: list[HistoricalRunTiming] = []
    for snapshot in context.storage.list_sessions():
        new_points = max(0, snapshot.completed - snapshot.already_measured)
        if snapshot.state in ACTIVE_SESSION_STATES or new_points < 5 or not snapshot.run_started_at:
            continue
        elapsed = elapsed_seconds(snapshot.run_started_at, ended_at=snapshot.updated_at)
        if elapsed is None or elapsed <= 0:
            continue
        try:
            request = context.storage.load_request(snapshot.id)
        except SESSION_LOAD_ERRORS:
            continue
        if not isinstance(request, LightMeasurementRequest) or request.fast_test_mode:
            continue
        runs.append(
            HistoricalRunTiming(
                session_id=snapshot.id,
                model_id=request.model_id,
                measure_device=request.measure_device,
                elapsed_seconds=elapsed,
                completed_points=new_points,
                modes=frozenset(request.modes),
                fast_test_mode=request.fast_test_mode,
            ),
        )
    return tuple(runs)


def _inherited_plot_keys(context: AppContext, snapshot: SessionSnapshot) -> dict[LutMode, set[Variation]]:
    """Seed LUT keys that this refine has not rewritten yet."""

    try:
        request = context.storage.load_request(snapshot.id)
    except SESSION_LOAD_ERRORS:
        return {}
    if not isinstance(request, LightMeasurementRequest):
        return {}
    if request.resume_policy != ResumePolicy.EXTEND or not request.seed_session_id:
        return {}
    seed = _seed_measured_variations(context, request.seed_session_id)
    collected = _variations_recorded_since(context, snapshot, request)
    return {mode: keys - collected.get(mode, set()) for mode, keys in seed.items()}


def _variations_recorded_since(
    context: AppContext,
    snapshot: SessionSnapshot,
    request: LightMeasurementRequest,
) -> dict[LutMode, set[Variation]]:
    started = raw_since_timestamp(snapshot)
    artifact = context.storage.artifact_directory(snapshot.id, request.model_id)
    found: dict[LutMode, set[Variation]] = {}
    for mode in LutMode:
        path = artifact / f"{mode.value}.raw.jsonl"
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(row.get("timestamp") or 0) < started:
                continue
            variation = _variation_from_raw(row.get("variation"), mode)
            if variation is not None:
                found.setdefault(mode, set()).add(variation)
    return found


def _variation_from_raw(data: object, mode: LutMode) -> Variation | None:
    if not isinstance(data, dict):
        return None
    try:
        if mode is LutMode.BRIGHTNESS:
            return Variation(bri=int(data["bri"]))
        if mode is LutMode.COLOR_TEMP:
            return ColorTempVariation(bri=int(data["bri"]), ct=int(data["ct"]))
        if mode is LutMode.HS:
            return HsVariation(bri=int(data["bri"]), hue=int(data["hue"]), sat=int(data["sat"]))
        if mode is LutMode.EFFECT:
            return EffectVariation(effect=str(data["effect"]), bri=int(data["bri"]))
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _seed_artifact_directory(context: AppContext, session_id: str) -> Path | None:
    try:
        request = context.storage.load_request(session_id)
    except SESSION_LOAD_ERRORS:
        return None
    if not isinstance(request, LightMeasurementRequest):
        return None
    return context.storage.artifact_directory(session_id, request.model_id)


def _seed_measured_variations(context: AppContext, session_id: str) -> dict[LutMode, set[Variation]]:
    directory = _seed_artifact_directory(context, session_id)
    return load_session_measured_variations(directory) if directory is not None else {}


def _seed_measured_points(context: AppContext, session_id: str) -> dict[LutMode, list[MeasuredPoint]]:
    directory = _seed_artifact_directory(context, session_id)
    if directory is None:
        return {}
    return {
        mode: [MeasuredPoint(variation, row.watt) for variation, row in rows.items()]
        for mode, rows in load_session_measured_rows(directory).items()
    }


def _apply_fast_test_mode(context: AppContext, request: MeasurementRequest) -> MeasurementRequest:
    settings = context.storage.load_settings()
    controller = request.controller
    supported_dummy_controller = controller is not None and controller.is_dummy
    enabled = (
        context.developer_mode
        and settings.fast_test_mode
        and isinstance(request.power_meter, DummyPowerMeterSpec)
        and supported_dummy_controller
    )
    parameters = replace(request.parameters, fast_test_mode=False)
    if enabled:
        parameters = replace(
            request.parameters,
            fast_test_mode=True,
            sleep_time=0,
            sleep_time_sample=0,
            sample_count=1,
            sleep_initial=0,
            sleep_standby=0,
            sleep_time_hue=0,
            sleep_time_sat=0,
            sleep_time_ct=0,
            sleep_time_effect_change=0,
            measure_time_effect=1,
            measure_time_effect_min=1,
        )
    return request.model_copy(update={"fast_test_mode": enabled, "parameters": parameters})


def _is_active(snapshot: SessionSnapshot | None) -> bool:
    return snapshot is not None and snapshot.state in ACTIVE_SESSION_STATES


def _measured_variations(context: AppContext, session_id: str, request: MeasurementRequest) -> list:
    if not isinstance(request, LightMeasurementRequest):
        return []
    try:
        by_mode = load_session_measured_variations(context.storage.artifact_directory(session_id, request.model_id))
    except OSError:
        return []
    return [variation for keys in by_mode.values() for variation in keys]


def _has_complete_lut_row(context: AppContext, session_id: str, request: MeasurementRequest) -> bool:
    return bool(_measured_variations(context, session_id, request))


def _can_refine(context: AppContext, snapshot: SessionSnapshot, request: MeasurementRequest) -> bool:
    return isinstance(request, LightMeasurementRequest) and context.storage.has_lut_csv(snapshot.id)


def _can_merge(context: AppContext, snapshot: SessionSnapshot, request: MeasurementRequest) -> bool:
    return _can_refine(context, snapshot, request) and not _is_active(snapshot)


def _sweep_coverage(context: AppContext, snapshot: SessionSnapshot) -> object:
    try:
        request = context.storage.load_request(snapshot.id)
        measured = _measured_variations(context, snapshot.id, request)
        return build_sweep_coverage(request, measured, snapshot.operating_point)
    except (SESSION_LOAD_ERRORS, OSError, ValueError):
        return None


def _duration_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([hms])", value)
    if match is None:
        return None
    multiplier = {"h": 3600, "m": 60, "s": 1}[match.group(2)]
    return round(float(match.group(1)) * multiplier)


def _snapshot_response(context: AppContext, snapshot: SessionSnapshot) -> SessionSnapshotResponse:
    elapsed, remaining = snapshot_progress_timing(snapshot)
    return SessionSnapshotResponse(
        session_id=snapshot.id,
        state=snapshot.state,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        phase=snapshot.phase,
        activity_reason=snapshot.activity_reason,
        confirmation_message=snapshot.confirmation_message,
        confirmation_action=snapshot.confirmation_action,
        mode=snapshot.mode,
        run_started_at=snapshot.run_started_at,
        wait_ends_at=snapshot.wait_ends_at,
        wait_seconds=snapshot.wait_seconds,
        progress=SessionProgressResponse(
            completed=snapshot.completed,
            total=snapshot.total,
            skipped=snapshot.skipped,
            already_measured=snapshot.already_measured,
            percent=snapshot.progress,
            elapsed_seconds=elapsed,
            estimated_remaining_seconds=remaining if remaining is not None else _duration_seconds(snapshot.estimated_remaining),
        ),
        warnings=list(snapshot.warnings),
        error=snapshot.error,
        summary=snapshot.summary,
        operating_point=snapshot.operating_point,
        calibration_sample=(
            CalibrationSampleResponse(**snapshot.calibration_sample)
            if snapshot.calibration_sample is not None
            else None
        ),
        entity_states=snapshot.entity_states,
        can_analyse=snapshot.state not in ACTIVE_SESSION_STATES and context.storage.can_analyse(snapshot.id),
        request=context.storage.load_request(snapshot.id),
        sweep_coverage=_sweep_coverage(context, snapshot),
    )


def _optional_entity_catalog(context: AppContext) -> EntityCatalogSnapshot | None:
    try:
        return HomeAssistantEntityCatalog(context.home_assistant).load_snapshot()
    except Exception as error:  # noqa: BLE001 - listing still works without live HA identity
        _LOGGER.warning("Could not resolve device identity for the session list: %s", error)
        return None


def _session_summaries(context: AppContext) -> list[SessionSummary]:
    """List cards from state.json + request.json. Do not parse LUT CSVs or raw logs."""

    catalog = _optional_entity_catalog(context)
    summaries = [_session_summary(context, snapshot, catalog) for snapshot in context.coordinator.sessions()]
    return sorted(summaries, key=lambda item: not item.active)


def _session_summary(
    context: AppContext,
    snapshot: SessionSnapshot,
    catalog: EntityCatalogSnapshot | None = None,
) -> SessionSummary:
    request = context.storage.load_request(snapshot.id)
    current = context.coordinator.current
    manufacturer, product, title = session_identity(request, descriptor_for_request(request, catalog))
    file_count, size = context.storage.session_listing_stats(snapshot.id)
    return SessionSummary(
        session_id=snapshot.id,
        state=snapshot.state,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        measure_type=request.measure_type,
        model_id=request.model_id,
        product_name=title,
        manufacturer=manufacturer,
        measure_device=request.measure_device,
        completed=snapshot.completed,
        total=snapshot.total,
        percent=snapshot.progress,
        can_resume=(
            snapshot.state in RESUMABLE_SESSION_STATES
            and isinstance(request, LightMeasurementRequest)
        ),
        can_refine=_can_refine(context, snapshot, request),
        can_merge=_can_merge(context, snapshot, request),
        file_count=file_count,
        size=size,
        active=current is not None and current.id == snapshot.id and _is_active(snapshot),
        family_key=session_family_key(request, product=product),
        modes=session_modes(request),
        run_started_at=snapshot.run_started_at,
        duration_seconds=session_duration_seconds(snapshot),
        already_measured=snapshot.already_measured,
        measured=points_this_run(
            completed=snapshot.completed,
            already_measured=snapshot.already_measured,
            recorded=None,
        ),
        seed_session_id=request.seed_session_id,
        can_analyse=snapshot.state not in ACTIVE_SESSION_STATES and context.storage.can_analyse(snapshot.id),
    )


def _has_raw_log(context: AppContext, snapshot: SessionSnapshot, request: MeasurementRequest) -> bool:
    if not isinstance(request, LightMeasurementRequest):
        return False
    artifact = context.storage.artifact_directory(snapshot.id, request.model_id)
    return any((artifact / f"{mode.value}.raw.jsonl").is_file() for mode in LutMode)


def _recorded_points_this_run(
    context: AppContext,
    snapshot: SessionSnapshot,
    request: MeasurementRequest,
) -> int | None:
    """Unique LUT keys written during this run, or None when there is no raw log."""

    if not isinstance(request, LightMeasurementRequest) or not _has_raw_log(context, snapshot, request):
        return None
    recorded = _variations_recorded_since(context, snapshot, request)
    return sum(len(keys) for keys in recorded.values())


def _file_descriptor(path: Path, name: str) -> SessionFile:
    return SessionFile(
        name=name,
        size=path.stat().st_size,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


async def _event_stream(request: Request, context: AppContext, session_id: str) -> AsyncIterator[str]:
    last_event_id = request.headers.get("last-event-id", "0")
    try:
        sequence = max(0, int(last_event_id))
    except ValueError:
        sequence = 0
    while not await request.is_disconnected():
        events = context.coordinator.events_since(sequence, session_id)
        if events:
            for event in events:
                sequence = max(sequence, event.sequence)
                yield _encode_event(context, event, session_id)
            # Comment frame so a proxy that was holding the small log/sample
            # payloads (no snapshot) actually flushes them.
            yield ": \n\n"
            await asyncio.sleep(_SSE_BUSY_SLEEP)
        else:
            try:
                snapshot = context.coordinator.get(session_id)
            except SESSION_LOAD_ERRORS:
                return
            heartbeat = SessionEventResponse(
                sequence=snapshot.event_sequence,
                type="heartbeat",
                data={},
                snapshot=_snapshot_response(context, snapshot),
            )
            yield f"event: heartbeat\ndata: {heartbeat.model_dump_json()}\n\n"
            await asyncio.sleep(_SSE_IDLE_SLEEP)


_SSE_BUSY_SLEEP = 0.05
_SSE_IDLE_SLEEP = 1.0
_SSE_SNAPSHOT_TYPES = frozenset(
    {
        SessionEventType.STATE,
        SessionEventType.PHASE,
        SessionEventType.PROGRESS,
        SessionEventType.CHECKPOINT,
        SessionEventType.OPERATING_POINT,
        SessionEventType.WARNING,
    }
)
_LOG_LINE_TYPES = frozenset({SessionEventType.LOG, SessionEventType.WARNING, SessionEventType.CHECKPOINT})


def _session_log_entries(
    context: AppContext,
    session_id: str,
    *,
    after: int = 0,
) -> list[dict[str, str | int]]:
    """The running view's log card. Same lines events.jsonl already stored.

    ``after`` is an event sequence: only newer log lines, served from the live
    in-memory ring when this session is the active one (no full jsonl scan).
    """

    events = (
        context.coordinator.events_since(after, session_id)
        if after > 0
        else context.storage.load_events(session_id, limit=5000)
    )
    lines: list[dict[str, str | int]] = []
    for event in events:
        if event.type not in _LOG_LINE_TYPES:
            continue
        message = event.data.get("message")
        if not isinstance(message, str) or not message:
            continue
        lines.append({"time": event.created_at, "message": message, "sequence": event.sequence})
    return lines


def _encode_event(context: AppContext, event: SessionEvent, session_id: str) -> str:
    payload = {
        "sequence": event.sequence,
        "type": event.type,
        "created_at": event.created_at,
        "data": event.data,
    }
    # Log/sample chatter is frequent. Rebuilding sweep coverage on every line
    # made the stream too heavy to flush, so the running view never saw it.
    if event.type in _SSE_SNAPSHOT_TYPES:
        try:
            snapshot = context.coordinator.get(session_id)
        except SESSION_LOAD_ERRORS:
            snapshot = None
        if snapshot is not None:
            payload["snapshot"] = _snapshot_response(context, snapshot).model_dump(mode="json")
    return f"id: {event.sequence}\nevent: {event.type}\ndata: {json.dumps(payload, default=str)}\n\n"
