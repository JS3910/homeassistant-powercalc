import { ApiError } from "./api-client";
import type { MeasureApiClient } from "./api-client";
import {
  hashFor,
  historyMode,
  locationFromApp,
  parseHash,
  silentHistory,
  type AppHistory,
  type HistoryLocation,
} from "./app-history";
import { entityDomains, requestFormData } from "./measurement/definition";
import { meterFor, ocrPreviewLabelsFromSpec, settingsInvolveOcr, specHasOcr } from "./power-meter/registry";
import { emptyPlots } from "./types";
import type {
  AppSettings,
  AppSettingsUpdate,
  Capabilities,
  ContributionAuthDeviceStatus,
  ContributionAuthState,
  ContributionDeviceFlow,
  ContributionFormValues,
  ContributionPreview,
  ContributionPreviewRequest,
  ContributionResult,
  ContributionStatus,
  ContributionSubmitRequest,
  DummyLoadCalibration,
  DeviceSpecificationField,
  EntityDescriptor,
  ErrorHelp,
  LogEntry,
  MeasureDefinition,
  MeasureType,
  MeasurementRequest,
  MergePreview,
  PlotCollection,
  PlotPointActionDetail,
  PowerMeterDiagnostic,
  PowerSample,
  PreflightResponse,
  SessionEvent,
  SessionFile,
  SessionSnapshot,
  SessionState,
  SessionSummary,
  SettingsSection,
  ShellyDiscoveryDevice,
} from "./types";

/** Live log lines kept in the running view. Events.jsonl still holds the full session. */
export const MAX_LIVE_LOGS = 5000;
/** Snapshot/progress catch-up when ingress drops EventSource. */
const SNAPSHOT_POLL_MS = 3_000;
/** Log lines are written every settle poll (~250 ms). Don't wait on the snapshot beat. */
const LOG_POLL_MS = 400;

export function mergeLogEntries(existing: LogEntry[], incoming: LogEntry[]): LogEntry[] {
  const seen = new Set<number>();
  const merged: LogEntry[] = [];
  for (const line of [...existing, ...incoming]) {
    if (line.sequence != null) {
      if (seen.has(line.sequence)) continue;
      seen.add(line.sequence);
    }
    merged.push(line);
  }
  return merged.slice(-MAX_LIVE_LOGS);
}

export type AppView =
  | "loading"
  | "sessions"
  | "setup"
  | "review"
  | "running"
  | "result"
  | "profile"
  | "submit"
  | "settings";

export interface MeasureAppState {
  view: AppView;
  setupDraftVersion?: number;
  lastEventReceivedAt?: string;
  lastAnalysedSessionId?: string;
  settingsSection?: SettingsSection;
  errorMessage: string;
  errorHelp?: ErrorHelp;
  busy: boolean;
  busyDetail: string;
  connectedToEvents: boolean;
  snapshot?: SessionSnapshot;
  sessions: SessionSummary[];
  request?: MeasurementRequest;
  selectedMeasureType?: MeasureType;
  preflight?: PreflightResponse;
  files: SessionFile[];
  plotCollection: PlotCollection;
  ocrPreviewLabels: string[];
  logs: LogEntry[];
  samples: PowerSample[];
  capabilities?: Capabilities;
  lights: EntityDescriptor[];
  powers: EntityDescriptor[];
  voltages: EntityDescriptor[];
  dummyLoadCalibration: DummyLoadCalibration | null;
  dummyLoadCalibrationError: string;
  settings?: AppSettings;
  measureDevices: string[];
  measureDevicesLoading: boolean;
  measureDevicesError: string;
  manufacturers?: string[];
  deviceSpecificationFields: Record<string, DeviceSpecificationField[]>;
  contributionAuth?: ContributionAuthState;
  contributionDeviceFlow?: ContributionDeviceFlow;
  contributionDeviceStatus?: ContributionAuthDeviceStatus;
  contributionDraft?: ContributionPreview;
  contributionFormValues?: ContributionFormValues;
  contributionPreview?: ContributionPreview;
  contributionResult?: ContributionResult;
  contributionBusy: boolean;
  contributionAuthBusy: boolean;
  contributionError: string;
  contributionErrorField?: string;
  contributionAuthError: string;
  definitions: MeasureDefinition[];
  deviceEntities: Record<string, EntityDescriptor[]>;
  deviceEntityErrors: Record<string, string>;
  testingPowerMeter: boolean;
  powerMeterTestResult?: PowerMeterDiagnostic;
  shellyDiscoveryDevices: ShellyDiscoveryDevice[];
  discoveringShellys: boolean;
  shellyDiscoveryError: string;
  shellyDiscoveryAvailable?: boolean;
  shellyDiscoveryMessage?: string | null;
  /** A throwaway live camera preview started while the settings form has an OCR meter
   * configured, so it can be pointed at the meter before any real session exists. `null`
   * when the currently configured meter has no OCR component (or nothing has been
   * addressed yet) -- see `MeterPreviewService`. */
  meterPreviewId: string | null;
  meterPreviewLabels: string[];
}

/**
 * Everything the controller calls on the API client. Derived from the client itself so the two
 * cannot drift; the URL builders are excluded because only the shell hands those to its views.
 */
export type MeasureAppApi = Omit<MeasureApiClient, "fileUrl" | "diagnosticsUrl" | "eventsUrl" | "preparedProfileUrl">;

export interface EventConnection {
  connect(): void;
  close(): void;
}

interface EventCallbacks {
  onEvent: (event: SessionEvent) => void;
  onConnection: (connected: boolean) => void;
  onReconnect: () => void;
}

type EventConnectionFactory = (sessionId: string, callbacks: EventCallbacks) => EventConnection;

/** Framework-neutral application controller. Lit only observes the state mutations. */
export class MeasureAppController {
  private eventConnection?: EventConnection;
  private settingsReturnView: AppView = "setup";
  private powerMeterTestVersion = 0;
  private meterPreviewVersion = 0;
  private shellyDiscoveryVersion = 0;
  private contributionDeviceFlowVersion = 0;
  private contributionDevicePollInterval = 0;
  private contributionDeviceExpiresAt = 0;
  private contributionDevicePollTimer?: ReturnType<typeof setTimeout>;
  private plotsPollTimer?: ReturnType<typeof setInterval>;
  private snapshotPollTimer?: ReturnType<typeof setInterval>;
  private logPollTimer?: ReturnType<typeof setInterval>;
  private readonly contributionTouchedFields = new Set<string>();
  private readonly history: AppHistory;
  private stopListeningHistory?: () => void;
  private lastHistory: HistoryLocation | null = null;
  private applyingHistory = false;
  private uiFlushQueued = false;

  constructor(
    private readonly state: MeasureAppState,
    private readonly api: () => MeasureAppApi,
    private readonly createEventConnection: EventConnectionFactory,
    private readonly emitChange: () => void,
    history?: AppHistory,
  ) {
    this.history = history ?? silentHistory();
    this.stopListeningHistory = this.history.listen((location) => {
      void this.handlePopState(location);
    });
  }

  private changed(): void {
    this.syncHistory();
    this.emitChange();
  }

  /** Coalesce chatter (settle logs, SSE + poll) into one Lit pass per turn. */
  private queueChanged(): void {
    if (this.uiFlushQueued) return;
    this.uiFlushQueued = true;
    queueMicrotask(() => {
      this.uiFlushQueued = false;
      this.changed();
    });
  }

  private clearError(): void {
    this.state.errorMessage = "";
    this.state.errorHelp = undefined;
  }

  private setError(error: unknown): void {
    this.state.errorMessage = message(error);
    this.state.errorHelp = error instanceof ApiError ? error.help : undefined;
  }

  dispose(): void {
    this.stopListeningHistory?.();
    this.stopListeningHistory = undefined;
    this.shellyDiscoveryVersion += 1;
    this.stopContributionDevicePolling();
    this.stopPlotsPolling();
    this.state.contributionAuthBusy = false;
    this.eventConnection?.close();
  }

  /**
   * Poll for the profile plot while a measurement is running, so the shape of the result
   * builds up on screen instead of only appearing once the whole run finishes. A slow
   * interval (not tied to individual sample events, which arrive far more often) keeps
   * this cheap: each poll re-parses the CSV(s) written so far.
   */
  private startPlotsPolling(): void {
    this.stopPlotsPolling();
    void this.refreshPlots();
    this.plotsPollTimer = setInterval(() => void this.refreshPlots(), 10_000);
    // HA ingress drops EventSource; the socket can also look open while events
    // stop. Poll the snapshot on its own beat so a finished run leaves "69%"
    // without waiting for F5.
    this.snapshotPollTimer = setInterval(() => {
      if (this.state.view === "running") void this.refreshSnapshot();
    }, SNAPSHOT_POLL_MS);
    this.logPollTimer = setInterval(() => {
      const sessionId = this.state.snapshot?.session_id;
      if (this.state.view === "running" && sessionId) {
        void this.refreshLogs(sessionId, { incremental: true });
      }
    }, LOG_POLL_MS);
  }

  private stopPlotsPolling(): void {
    if (this.plotsPollTimer !== undefined) {
      clearInterval(this.plotsPollTimer);
      this.plotsPollTimer = undefined;
    }
    if (this.snapshotPollTimer !== undefined) {
      clearInterval(this.snapshotPollTimer);
      this.snapshotPollTimer = undefined;
    }
    if (this.logPollTimer !== undefined) {
      clearInterval(this.logPollTimer);
      this.logPollTimer = undefined;
    }
  }

  /**
   * OCR camera preview(s) a session assembled, if any -- which meters (and thus which
   * labels) exist is fixed for the lifetime of a session, unlike the plot data itself, so
   * this stops retrying as soon as it finds any.
   *
   * Session creation returns as soon as the session is queued, well before the backend
   * has actually assembled the power meter and registered its OCR preview(s) -- probing
   * a device or connecting to an entity can easily take longer than that. A single fetch
   * right when the running view connects routinely lost that race, leaving the camera
   * preview missing until something else (e.g. a page reload re-running this same fetch,
   * by then too late to matter) happened to ask again. Retrying for a while covers the
   * gap without polling forever once a session's answer is settled.
   */
  private async loadOcrPreviewLabels(sessionId: string): Promise<void> {
    const attempts = 10;
    const delayMs = 750; // ~7.5s worst case, comfortably past assembly for any real meter
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (this.state.snapshot?.session_id !== sessionId) return; // navigated away meanwhile
      try {
        const labels = await this.api().getOcrPreviewLabels(sessionId);
        if (labels.length > 0) {
          this.state.ocrPreviewLabels = labels;
          this.changed();
          return;
        }
      } catch {
        // Same "not registered yet" gap as an empty list while the assembler is still
        // running -- keep retrying on the same schedule rather than giving up early.
      }
      if (attempt < attempts - 1) await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
    if (!this.state.ocrPreviewLabels.length) {
      this.state.ocrPreviewLabels = this.ocrLabelsFromRequest();
    }
    this.changed();
  }

  private ocrLabelsFromRequest(): string[] {
    return ocrPreviewLabelsFromSpec(this.state.snapshot?.request?.power_meter ?? this.state.request?.power_meter);
  }

  async applyPlotPointAction(detail: PlotPointActionDetail): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    await this.run(async () => {
      this.state.plotCollection = await this.api().editPlotPoint(sessionId, detail);
    });
  }

  private async refreshPlots(): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    try {
      this.state.plotCollection = await this.api().getPlots(sessionId);
      this.changed();
    } catch {
      // Expected 409 before any reading has been taken yet; keep whatever was last shown.
    }
    // Plots poll the CSV even if the SSE stream has gone quiet (HA ingress idle
    // timeouts). Refresh the snapshot on the same beat so variation/time don't freeze.
    if (this.state.view === "running") await this.refreshSnapshot();
  }

  async boot(): Promise<void> {
    this.state.view = "loading";
    this.clearError();
    this.changed();
    try {
      const api = this.api();
      const calibrationPromise = this.refreshDummyLoadCalibration();
      const [capabilities, entities, settings, auth, sessions, definitions] = await Promise.all([
        api.getCapabilities(),
        api.getEntityCatalog(),
        api.getSettings(),
        api.getContributionAuth().catch(() => ({ connected: false }) satisfies ContributionAuthState),
        api.getSessions(),
        api.getMeasureDefinitions(),
      ]);
      this.state.capabilities = capabilities;
      this.state.lights = entities.lights;
      this.state.powers = entities.powers;
      this.state.voltages = entities.voltages;
      this.state.settings = settings;
      this.state.contributionAuth = auth;
      this.state.sessions = sessions;
      const active = sessions.find((session) => session.active);
      this.state.snapshot = active ? await api.getSession(active.session_id) : undefined;
      this.state.definitions = definitions;
      await calibrationPromise;
      this.state.request = this.state.snapshot?.request;
      if (this.state.request) await this.loadTypeEntities(this.state.request.measure_type);
      await this.routeSnapshot();
      const fromHash = parseHash(this.history.getHash());
      if (fromHash && fromHash.view !== this.state.view) {
        await this.applyHistoryLocation(fromHash);
      }
    } catch (error) {
      this.setError(error);
    }
    this.changed();
  }

  selectMeasureType(type: MeasureType): void {
    this.state.selectedMeasureType = type;
    this.changed();
    void this.loadTypeEntities(type);
  }

  loadEntityDomains(domains: string[]): void {
    void this.ensureEntityDomains(domains);
  }

  async preflight(request: MeasurementRequest): Promise<void> {
    this.state.request = this.preserveRefineFields(request);
    let startImmediately = false;
    await this.run(async () => {
      this.state.preflight = await this.api().preflight(this.state.request!);
      if (this.shouldStartImmediately(this.state.preflight, this.state.request!)) {
        startImmediately = true;
        return;
      }
      this.state.view = "review";
    });
    if (this.state.view === "setup" || this.state.view === "review") {
      await this.ensureSetupMeterPreview();
    }
    if (startImmediately) await this.start();
  }

  /** Review is only for a warning, dummy load, or a type that needs a physical confirm. */
  private shouldStartImmediately(
    preflight: { valid: boolean; warnings: string[] },
    request: MeasurementRequest,
  ): boolean {
    if (!preflight.valid || preflight.warnings.length) return false;
    if (request.dummy_load) return false;
    const definition = this.state.definitions.find((candidate) => candidate.measure_type === request.measure_type);
    return !definition?.confirmation_action && !definition?.confirmation_is_warning;
  }

  backToSetup(): void {
    this.clearError();
    this.state.setupDraftVersion = (this.state.setupDraftVersion ?? 0) + 1;
    this.state.view = "setup";
    this.changed();
    void this.ensureSetupMeterPreview();
  }

  async start(): Promise<void> {
    const request = this.state.request;
    if (!request) return;
    this.state.samples = [];
    this.state.plotCollection = emptyPlots();
    // The aiming preview holds 127.0.0.1:8765; the real session builds its own meter
    // on that port. Wait for the preview to release it before assemble() runs.
    await this.stopMeterPreview();
    await this.run(async () => {
      this.state.snapshot = await this.api().start(request);
      await this.enterRunning();
    });
    if (this.state.view !== "running") {
      await this.ensureSetupMeterPreview();
    }
  }

  async confirm(): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    await this.sessionCommand("Confirmation", () => this.api().confirm(sessionId));
  }

  async cancel(): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    await this.sessionCommand("Stop", () => this.api().cancel(sessionId));
  }

  async resume(): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    this.state.plotCollection = emptyPlots();
    this.state.lastEventReceivedAt = undefined;
    this.dismissResumeFailure();
    await this.run(async () => {
      this.state.snapshot = await this.api().resume(sessionId);
      await this.enterRunning();
    });
  }


  async analyseRecording(): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    this.state.lastAnalysedSessionId = undefined;
    await this.run(async () => {
      this.state.snapshot = await this.api().analyse(sessionId);
      await this.loadResultArtifacts();
      await this.refreshSessions();
      this.state.lastAnalysedSessionId = sessionId;
    });
  }

  newMeasurement(): void {
    this.resetDraft();
    this.state.setupDraftVersion = (this.state.setupDraftVersion ?? 0) + 1;
    this.state.view = "setup";
    this.changed();
    void this.ensureSetupMeterPreview();
  }

  openProfile(): void {
    if (this.state.snapshot?.state !== "completed" || this.isAverageMeasurement()) return;
    this.clearError();
    this.state.view = "profile";
    this.changed();
  }

  openSubmit(): void {
    if (this.state.snapshot?.state !== "completed" || !this.state.contributionPreview || this.isAverageMeasurement())
      return;
    if (Object.keys(this.state.contributionFormValues ?? {}).length) return;
    this.clearError();
    this.state.view = "submit";
    this.changed();
  }

  backToProfile(): void {
    if (this.isAverageMeasurement()) return;
    this.clearError();
    this.state.view = "profile";
    this.changed();
  }

  private isAverageMeasurement(): boolean {
    return (
      (this.state.snapshot?.request?.measure_type ??
        this.state.request?.measure_type ??
        this.state.selectedMeasureType) === "average"
    );
  }

  backToResult(): void {
    this.clearError();
    this.state.view = "result";
    this.changed();
  }

  replaceDraftEnvironment(powerMeter: MeasurementRequest["power_meter"], measureDevice: string): void {
    if (!this.state.request) return;
    this.state.request = {
      ...this.state.request,
      power_meter: powerMeter,
      measure_device: measureDevice,
    };
    this.changed();
  }

  async showSessions(): Promise<void> {
    this.eventConnection?.close();
    this.state.connectedToEvents = false;
    this.stopMeterPreview();
    await this.run(async () => {
      await this.refreshSessions();
      this.state.view = "sessions";
    });
  }

  async openSession(sessionId: string): Promise<void> {
    await this.run(async () => {
      const snapshot = await this.api().getSession(sessionId);
      this.state.snapshot = snapshot;
      await this.adoptRequest(snapshot.request);
      if (isActive(snapshot.state)) {
        this.state.view = "running";
        this.connectEvents();
      } else {
        await this.enterResult();
      }
    });
  }

  async resumeSession(sessionId: string): Promise<void> {
    this.state.plotCollection = emptyPlots();
    if (this.state.snapshot?.session_id === sessionId) {
      this.dismissResumeFailure();
    }
    await this.run(async () => {
      const snapshot = await this.api().resume(sessionId);
      this.state.snapshot = snapshot;
      await this.adoptRequest(snapshot.request);
      await this.enterRunning();
    });
  }

  async duplicateSession(sessionId: string): Promise<void> {
    await this.run(async () => {
      const snapshot = await this.api().getSession(sessionId);
      if (!snapshot.request) throw new Error("The stored session has no reusable configuration.");
      const draft = {
        ...snapshot.request,
        resume_policy: "new" as const,
        seed_session_id: undefined,
        remeasure_existing: false,
      };
      this.resetDraft(draft);
      await this.loadTypeEntities(draft.measure_type, draft);
      this.state.view = "setup";
      await this.ensureSetupMeterPreview();
    });
  }

  async refineSession(sessionId: string): Promise<void> {
    await this.run(async () => {
      const snapshot = await this.api().getSession(sessionId);
      if (!snapshot.request) throw new Error("The stored session has no reusable configuration.");
      const draft = {
        ...snapshot.request,
        resume_policy: "extend" as const,
        seed_session_id: sessionId,
        remeasure_existing: false,
      };
      this.resetDraft(draft);
      await this.loadTypeEntities(draft.measure_type, draft);
      this.state.view = "setup";
      await this.ensureSetupMeterPreview();
    });
  }

  setRemeasureExisting(enabled: boolean): void {
    const request = this.state.request;
    if (!request || request.resume_policy !== "extend") return;
    this.state.request = { ...request, remeasure_existing: enabled };
    this.changed();
  }

  async previewMerge(rightId: string): Promise<MergePreview> {
    const left = this.state.snapshot?.session_id;
    if (!left) throw new Error("No session is open to merge.");
    return this.api().previewMerge(left, rightId);
  }

  async confirmMerge(rightId: string): Promise<void> {
    const left = this.state.snapshot?.session_id;
    if (!left) return;
    await this.run(async () => {
      const snapshot = await this.api().mergeSessions(left, rightId);
      this.state.snapshot = snapshot;
      await this.adoptRequest(snapshot.request);
      await this.enterResult();
    });
  }

  async deleteSession(sessionId: string): Promise<void> {
    await this.run(async () => {
      await this.api().deleteSession(sessionId);
      if (this.state.snapshot?.session_id === sessionId) this.state.snapshot = undefined;
      await this.refreshSessions();
    });
  }

  openSettings(section?: SettingsSection): void {
    if (this.state.view === "loading" || this.state.view === "settings") return;
    this.settingsReturnView = this.state.view;
    this.state.settingsSection = section;
    this.clearError();
    this.powerMeterTestVersion += 1;
    this.state.powerMeterTestResult = undefined;
    this.state.testingPowerMeter = false;
    this.state.view = "settings";
    this.changed();
    void this.loadMeasureDevices();
    const meter = this.state.settings?.power_meter;
    if (meter && meterFor(meter).discoverable) void this.discoverShellys();
  }

  closeSettings(): void {
    if (!this.applyingHistory && this.lastHistory?.view === "settings" && this.history.back()) {
      return;
    }
    this.leaveSettings(this.settingsReturnView);
  }

  private async loadMeasureDevices(): Promise<void> {
    this.state.measureDevicesLoading = true;
    this.state.measureDevicesError = "";
    this.changed();
    try {
      this.state.measureDevices = (await this.api().getMeasureDevices()).devices;
    } catch (error) {
      this.state.measureDevicesError = message(error);
    } finally {
      this.state.measureDevicesLoading = false;
      this.changed();
    }
  }

  async testPowerMeter(settings: AppSettingsUpdate): Promise<void> {
    const version = ++this.powerMeterTestVersion;
    this.state.testingPowerMeter = true;
    this.state.powerMeterTestResult = undefined;
    this.changed();
    try {
      const result = await this.api().testPowerMeter(settings);
      if (version === this.powerMeterTestVersion) this.state.powerMeterTestResult = result;
    } catch (error) {
      if (version === this.powerMeterTestVersion) {
        this.state.powerMeterTestResult = {
          success: false,
          status: "poor",
          reports_observed: 0,
          duration_seconds: 0,
          precision_status: "unsupported",
          update_interval_status: "unsupported",
          messages: [],
          message: message(error),
        };
      }
    } finally {
      if (version === this.powerMeterTestVersion) {
        this.state.testingPowerMeter = false;
        this.changed();
      }
    }
  }

  clearPowerMeterTestResult(): void {
    this.powerMeterTestVersion += 1;
    this.state.testingPowerMeter = false;
    this.state.powerMeterTestResult = undefined;
    this.changed();
  }

  /**
   * Start (or replace) the live OCR camera preview for whatever the settings form
   * currently describes, so the camera can be aimed before any real session exists. Safe
   * to call on every relevant form edit -- superseded and no-preview outcomes both clean
   * up whatever was previously running rather than leaking it.
   */
  async startMeterPreview(settings: AppSettingsUpdate): Promise<void> {
    const version = ++this.meterPreviewVersion;
    const previousId = this.state.meterPreviewId;
    try {
      const result = await this.api().startMeterPreview(settings);
      if (version !== this.meterPreviewVersion) {
        // A newer request (or an explicit stop) landed while this one was in flight --
        // don't resurrect a preview the user has already moved past.
        if (result.preview_id) void this.api().stopMeterPreview(result.preview_id);
        return;
      }
      this.state.meterPreviewId = result.preview_id;
      this.state.meterPreviewLabels = result.labels;
      this.changed();
      if (previousId && previousId !== result.preview_id) void this.api().stopMeterPreview(previousId);
    } catch {
      // Best-effort UI convenience for aiming a camera; a failed attempt shouldn't error the form.
    }
  }

  stopMeterPreview(): Promise<void> {
    this.meterPreviewVersion += 1;
    const previewId = this.state.meterPreviewId;
    this.state.meterPreviewId = null;
    this.state.meterPreviewLabels = [];
    this.changed();
    return previewId ? this.api().stopMeterPreview(previewId) : Promise.resolve();
  }

  /**
   * Rebuild a dead aiming preview from the current Defaults. Does not touch the
   * setup form, the draft request, or the current view.
   */
  async reconnectMeterPreview(): Promise<void> {
    const settings = this.state.settings;
    if (!settings || (!settingsInvolveOcr(settings) && !specHasOcr(this.state.request?.power_meter))) {
      return;
    }
    await this.startMeterPreview(settings);
  }

  async discoverShellys(): Promise<void> {
    const version = ++this.shellyDiscoveryVersion;
    this.state.discoveringShellys = true;
    this.state.shellyDiscoveryError = "";
    this.changed();
    try {
      const result = await this.api().getShellyDevices();
      if (version !== this.shellyDiscoveryVersion) return;
      this.state.shellyDiscoveryDevices = result.devices;
      this.state.shellyDiscoveryAvailable = result.available;
      this.state.shellyDiscoveryMessage = result.message;
    } catch (error) {
      if (version !== this.shellyDiscoveryVersion) return;
      this.state.shellyDiscoveryError = message(error);
    } finally {
      if (version === this.shellyDiscoveryVersion) {
        this.state.discoveringShellys = false;
        this.changed();
      }
    }
  }

  async saveSettings(settings: AppSettingsUpdate): Promise<void> {
    await this.run(async () => {
      this.state.settings = await this.api().saveSettings(settings);
      [this.state.capabilities] = await Promise.all([
        this.api().getCapabilities(),
        this.refreshDummyLoadCalibration(),
        this.refreshContributionDefaults(),
      ]);
      this.state.view = this.settingsReturnView;
    });
  }

  private async refreshContributionDefaults(): Promise<void> {
    const previous = this.state.contributionDraft;
    const sessionId = this.state.snapshot?.session_id;
    if (!previous || !sessionId) return;
    const defaults = await this.api().getContributionDraft(sessionId);
    if (this.state.snapshot?.session_id !== sessionId) return;
    const current = this.state.contributionPreview ?? previous;
    const merged = { ...current };
    let changed = false;
    for (const field of [
      "contributor",
      "contributor_github",
      "contributor_email",
      "measure_device_firmware",
    ] as const) {
      // Keep both edits made here (including explicit blanks) and overrides in a restored preview.
      if ((current[field] ?? "") !== (previous[field] ?? "")) this.contributionTouchedFields.add(field);
      if (this.contributionTouchedFields.has(field)) continue;
      if ((current[field] ?? "") === (defaults[field] ?? "")) continue;
      merged[field] = defaults[field] ?? "";
      changed = true;
    }
    if (changed) {
      this.state.contributionDraft = merged;
      this.state.contributionPreview = undefined;
      this.state.contributionResult = undefined;
      this.state.contributionError = "";
      this.state.contributionErrorField = undefined;
      if (this.settingsReturnView === "submit") this.settingsReturnView = "profile";
    }
  }

  editContribution(values: ContributionFormValues): void {
    this.state.contributionFormValues = values;
    for (const field of Object.keys(values)) this.contributionTouchedFields.add(field);
    this.changed();
  }

  async startContributionDeviceAuth(): Promise<void> {
    this.stopContributionDevicePolling();
    const version = this.contributionDeviceFlowVersion;
    this.state.contributionAuthBusy = true;
    this.state.contributionAuthError = "";
    this.state.contributionDeviceFlow = undefined;
    this.state.contributionDeviceStatus = undefined;
    this.changed();
    try {
      const flow = await this.api().startContributionDeviceAuth();
      if (version !== this.contributionDeviceFlowVersion) return;
      this.state.contributionDeviceFlow = flow;
      this.state.contributionDeviceStatus = {
        status: "pending",
        message: "Waiting for GitHub authorization…",
      };
      this.contributionDevicePollInterval = Math.max(1, flow.interval);
      this.contributionDeviceExpiresAt = Date.now() + Math.max(0, flow.expires_in) * 1_000;
      this.scheduleContributionDevicePoll(version);
    } catch (error) {
      if (version !== this.contributionDeviceFlowVersion) return;
      this.state.contributionAuthError = message(error);
    } finally {
      if (version === this.contributionDeviceFlowVersion) {
        this.state.contributionAuthBusy = false;
        this.changed();
      }
    }
  }

  private async pollContributionDeviceAuth(version: number): Promise<void> {
    const flowId = this.state.contributionDeviceFlow?.flow_id;
    if (!flowId || version !== this.contributionDeviceFlowVersion) return;
    if (Date.now() >= this.contributionDeviceExpiresAt) {
      this.expireContributionDeviceFlow();
      return;
    }
    try {
      const status = await this.api().getContributionDeviceAuth(flowId);
      if (this.isStaleContributionDevicePoll(version, flowId)) return;
      this.applyContributionDeviceStatus(status, version);
    } catch (error) {
      if (this.isStaleContributionDevicePoll(version, flowId)) return;
      this.handleContributionDevicePollError(error, version);
    } finally {
      if (version === this.contributionDeviceFlowVersion) this.changed();
    }
  }

  /** A poll result is stale when the flow was restarted or replaced while the request was in flight. */
  private isStaleContributionDevicePoll(version: number, flowId: string): boolean {
    return version !== this.contributionDeviceFlowVersion || flowId !== this.state.contributionDeviceFlow?.flow_id;
  }

  private applyContributionDeviceStatus(status: ContributionAuthDeviceStatus, version: number): void {
    this.state.contributionDeviceStatus = status;
    if (status.auth) this.state.contributionAuth = status.auth;
    this.state.contributionAuthError = "";

    if (status.status === "authorized") {
      this.state.contributionDeviceFlow = undefined;
      this.stopContributionDevicePolling();
      this.changed();
      return;
    }

    if (status.status !== "pending" && status.status !== "slow_down") return;

    if (status.status === "slow_down") {
      this.contributionDevicePollInterval = validRetryAfter(status.retry_after)
        ? Math.max(this.contributionDevicePollInterval, status.retry_after)
        : this.contributionDevicePollInterval + 5;
    }
    this.scheduleContributionDevicePoll(version);
  }

  private handleContributionDevicePollError(error: unknown, version: number): void {
    if (error instanceof ApiError && error.status === 404) {
      this.expireContributionDeviceFlow();
      return;
    }
    this.state.contributionAuthError = message(error);
    this.scheduleContributionDevicePoll(version);
  }

  private scheduleContributionDevicePoll(version: number): void {
    this.clearContributionDevicePollTimer();
    if (version !== this.contributionDeviceFlowVersion || !this.state.contributionDeviceFlow) return;
    const remaining = this.contributionDeviceExpiresAt - Date.now();
    if (remaining <= 0) {
      this.expireContributionDeviceFlow();
      return;
    }
    const delay = Math.min(this.contributionDevicePollInterval * 1_000, remaining);
    this.contributionDevicePollTimer = setTimeout(() => {
      this.contributionDevicePollTimer = undefined;
      void this.pollContributionDeviceAuth(version);
    }, delay);
  }

  private expireContributionDeviceFlow(): void {
    this.clearContributionDevicePollTimer();
    this.state.contributionDeviceStatus = {
      status: "expired",
      message: "This GitHub code expired. Request a new code to continue.",
    };
    this.changed();
  }

  private stopContributionDevicePolling(): void {
    this.contributionDeviceFlowVersion += 1;
    this.clearContributionDevicePollTimer();
  }

  private clearContributionDevicePollTimer(): void {
    if (this.contributionDevicePollTimer === undefined) return;
    clearTimeout(this.contributionDevicePollTimer);
    this.contributionDevicePollTimer = undefined;
  }

  async saveContributionToken(token: string): Promise<void> {
    this.state.contributionAuthBusy = true;
    this.state.contributionAuthError = "";
    this.changed();
    try {
      this.state.contributionAuth = await this.api().saveContributionToken(token);
      this.stopContributionDevicePolling();
      this.state.contributionDeviceFlow = undefined;
      this.state.contributionDeviceStatus = undefined;
    } catch (error) {
      this.state.contributionAuthError = message(error);
    } finally {
      this.state.contributionAuthBusy = false;
      this.changed();
    }
  }

  async disconnectContributionAuth(): Promise<void> {
    this.state.contributionAuthBusy = true;
    this.state.contributionAuthError = "";
    this.changed();
    try {
      this.state.contributionAuth = await this.api().disconnectContributionAuth();
      this.stopContributionDevicePolling();
      this.state.contributionDeviceFlow = undefined;
      this.state.contributionDeviceStatus = undefined;
    } catch (error) {
      this.state.contributionAuthError = message(error);
    } finally {
      this.state.contributionAuthBusy = false;
      this.changed();
    }
  }

  async previewContribution(request: ContributionPreviewRequest): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    await this.runContribution(async () => {
      this.state.contributionPreview = await this.api().previewContribution(sessionId, request);
      this.state.contributionFormValues = undefined;
      this.state.contributionResult = undefined;
    });
  }

  async submitContribution(request: ContributionSubmitRequest): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    await this.runContribution(async () => {
      this.state.contributionResult = await this.api().submitContribution(sessionId, request);
    });
  }

  async retryDummyLoadCalibration(): Promise<void> {
    await this.refreshDummyLoadCalibration();
    this.changed();
  }

  private async refreshDummyLoadCalibration(): Promise<void> {
    try {
      this.state.dummyLoadCalibration = await this.api().getDummyLoadCalibration();
      this.state.dummyLoadCalibrationError = "";
    } catch (error) {
      this.state.dummyLoadCalibrationError = `Could not load the saved dummy-load calibration: ${message(error)}`;
    }
  }

  private async loadTypeEntities(type: MeasureType, request?: MeasurementRequest): Promise<void> {
    const definition = this.state.definitions.find((candidate) => candidate.measure_type === type);
    if (!definition) return;
    // A restored request may make fields visible that the type's defaults do not.
    const values = request ? requestFormData(definition, request) : undefined;
    await this.ensureEntityDomains(entityDomains(definition, values));
  }

  private async ensureEntityDomains(domains: string[]): Promise<void> {
    const pending = [...new Set(domains)].filter((domain) => !(domain in this.state.deviceEntities));
    if (!pending.length) return;
    const results = await Promise.allSettled(
      pending.map((domain) => (domain === "*" ? this.api().getAllEntities() : this.api().getEntitiesByDomain(domain))),
    );
    results.forEach((result, index) => {
      const domain = pending[index];
      if (!domain) return;
      if (result.status === "fulfilled") {
        this.state.deviceEntities = {
          ...this.state.deviceEntities,
          [domain]: result.value,
        };
        const { [domain]: _, ...remainingErrors } = this.state.deviceEntityErrors;
        this.state.deviceEntityErrors = remainingErrors;
      } else {
        this.state.deviceEntityErrors = {
          ...this.state.deviceEntityErrors,
          [domain]: message(result.reason),
        };
      }
    });
    this.changed();
  }

  private async routeSnapshot(): Promise<void> {
    const state = this.state.snapshot?.state ?? "idle";
    if (isActive(state)) {
      this.state.view = "running";
      this.connectEvents();
      return;
    }
    this.state.view = "sessions";
  }

  private connectEvents(): void {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    this.eventConnection?.close();
    this.startPlotsPolling();
    void this.loadOcrPreviewLabels(sessionId);
    this.eventConnection = this.createEventConnection(sessionId, {
      onEvent: (event) => this.consumeEvent(event),
      onConnection: (connected) => {
        this.state.connectedToEvents = connected;
        this.changed();
      },
      onReconnect: () => {
        void this.refreshSnapshot();
      },
    });
    this.eventConnection.connect();
    void this.refreshLogs(sessionId);
  }

  private consumeEvent(event: SessionEvent): void {
    if ((event.type === "log" || event.type === "warning" || event.type === "checkpoint") && event.data.message) {
      this.state.logs = mergeLogEntries(this.state.logs, [
        { time: event.created_at ?? "", message: event.data.message, sequence: event.sequence },
      ]);
    }
    if (event.type === "sample" && typeof event.data.power === "number") {
      this.state.samples = [...this.state.samples.slice(-179), { power: event.data.power, at: event.created_at }];
    }
    if (event.snapshot) this.state.snapshot = event.snapshot;
    this.queueChanged();
    if (this.state.snapshot && isTerminal(this.state.snapshot.state)) void this.enterResult();
  }

  private async refreshSnapshot(): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    try {
      this.state.snapshot = await this.api().getSession(sessionId);
      if (isTerminal(this.state.snapshot.state)) await this.enterResult();
    } catch {
      this.state.connectedToEvents = false;
    }
    await this.refreshLogs(sessionId);
    this.changed();
  }

  private async refreshLogs(sessionId: string, options?: { incremental?: boolean }): Promise<void> {
    const after = options?.incremental ? Math.max(0, ...this.state.logs.map((line) => line.sequence ?? 0)) : 0;
    try {
      const logs = await this.api().getSessionLogs(sessionId, after);
      if (!logs.length) return;
      this.state.logs = options?.incremental ? mergeLogEntries(this.state.logs, logs) : logs.slice(-MAX_LIVE_LOGS);
    } catch {
      // Keep whatever the event stream last delivered.
    }
    if (options?.incremental) this.queueChanged();
  }

  private async enterResult(): Promise<void> {
    this.eventConnection?.close();
    this.stopPlotsPolling();
    this.state.connectedToEvents = false;
    this.state.ocrPreviewLabels = [];
    const sessionId = this.state.snapshot?.session_id;
    if (sessionId) {
      try {
        const latest = await this.api().getSession(sessionId);
        this.state.snapshot = {
          ...latest,
          error: latest.error ?? this.state.snapshot?.error,
        };
      } catch {
        // Keep the last in-memory snapshot so the result page still renders.
      }
    }
    if (this.state.view === "settings") this.settingsReturnView = "result";
    else this.state.view = "result";
    await this.loadResultArtifacts();
    await this.refreshSessions();
    this.changed();
  }

  private async refreshSessions(): Promise<void> {
    try {
      this.state.sessions = await this.api().getSessions();
    } catch (error) {
      this.state.errorMessage ||= `Could not refresh measurement sessions: ${message(error)}`;
    }
  }

  private resetDraft(request?: MeasurementRequest): void {
    this.eventConnection?.close();
    this.stopPlotsPolling();
    this.state.connectedToEvents = false;
    this.state.snapshot = { state: "idle" };
    this.state.request = request;
    this.state.selectedMeasureType = request?.measure_type;
    this.state.preflight = undefined;
    this.state.files = [];
    this.state.plotCollection = emptyPlots();
    this.state.ocrPreviewLabels = [];
    this.state.logs = [];
    this.state.samples = [];
    this.state.contributionDraft = undefined;
    this.state.contributionFormValues = undefined;
    this.contributionTouchedFields.clear();
    this.state.contributionPreview = undefined;
    this.state.contributionResult = undefined;
    this.state.contributionError = "";
    this.state.contributionErrorField = undefined;
    this.clearError();
  }

  /**
   * Shared shape of every user-triggered command: mark the app busy, report a failure in the
   * error banner, and notify the view once before and once after the work.
   */
  private async run(work: () => Promise<void>): Promise<void> {
    this.state.busy = true;
    this.clearError();
    this.changed();
    try {
      await work();
    } catch (error) {
      this.setError(error);
    } finally {
      this.state.busy = false;
      this.state.busyDetail = "";
      this.changed();
    }
  }

  /** The same, for contribution work, which reports into its own busy flag and error banner. */
  private async runContribution(work: () => Promise<void>): Promise<void> {
    this.state.contributionBusy = true;
    this.state.contributionError = "";
    this.state.contributionErrorField = undefined;
    this.changed();
    try {
      await work();
    } catch (error) {
      this.state.contributionError = message(error);
      this.state.contributionErrorField = error instanceof ApiError ? (error.field ?? undefined) : undefined;
    } finally {
      this.state.contributionBusy = false;
      this.changed();
    }
  }

  /** Keep extend/seed fields when setup rebuilds a request from the form. */
  private preserveRefineFields(request: MeasurementRequest): MeasurementRequest {
    const draft = this.state.request;
    if (draft?.resume_policy !== "extend" || !draft.seed_session_id) return request;
    return {
      ...request,
      resume_policy: "extend",
      seed_session_id: draft.seed_session_id,
      remeasure_existing: draft.remeasure_existing ?? request.remeasure_existing ?? false,
    };
  }

  /** Adopt the configuration a stored session was started with, so the draft and forms match it. */
  private async adoptRequest(request?: MeasurementRequest): Promise<void> {
    this.state.request = request;
    if (request) await this.loadTypeEntities(request.measure_type, request);
  }

  /** Drop the recorded failure so Resume does not keep showing it as a fresh error. */
  private dismissResumeFailure(): void {
    const snapshot = this.state.snapshot;
    if (!snapshot) return;
    this.state.snapshot = {
      ...snapshot,
      error: undefined,
      warnings: [],
    };
  }

  private async enterRunning(): Promise<void> {
    this.stopMeterPreview();
    if (!this.state.ocrPreviewLabels?.length) {
      this.state.ocrPreviewLabels = this.ocrLabelsFromRequest();
    }
    await this.refreshSessions();
    this.state.view = "running";
    this.connectEvents();
  }

  /**
   * Start the throwaway OCR aiming preview for the current Defaults/draft, if a camera
   * is in the plan and one isn't already running. Safe to call on every entry to
   * setup/review -- a no-OCR plan stops whatever was left over, and an already-running
   * preview is left alone rather than torn down and rebuilt.
   */
  async ensureSetupMeterPreview(): Promise<void> {
    const settings = this.state.settings;
    if (!settings || (!settingsInvolveOcr(settings) && !specHasOcr(this.state.request?.power_meter))) {
      if (this.state.meterPreviewId) this.stopMeterPreview();
      return;
    }
    if (this.state.meterPreviewId) return;
    await this.startMeterPreview(settings);
  }

  private async sessionCommand(label: string, command: () => Promise<SessionSnapshot>): Promise<void> {
    this.state.busy = true;
    this.changed();
    try {
      this.state.snapshot = await command();
    } catch (error) {
      this.state.logs = [
        ...this.state.logs.slice(-(MAX_LIVE_LOGS - 1)),
        {
          time: new Date().toISOString(),
          message: `${label} failed: ${message(error)}`,
        },
      ];
    } finally {
      this.state.busy = false;
      this.changed();
    }
  }

  private async loadResultArtifacts(): Promise<void> {
    const sessionId = this.state.snapshot?.session_id;
    if (!sessionId) return;
    this.state.contributionFormValues = undefined;
    this.contributionTouchedFields.clear();
    const [
      files,
      plots,
      calibration,
      auth,
      contribution,
      contributionStatus,
      manufacturers,
      deviceSpecifications,
      measureDevices,
    ] = await Promise.allSettled([
      this.api().getFiles(sessionId),
      this.api().getPlots(sessionId),
      this.api().getDummyLoadCalibration(),
      this.api().getContributionAuth(),
      this.api().getContributionDraft(sessionId),
      this.api().getContributionStatus(),
      this.api().getManufacturers(),
      this.api().getDeviceSpecifications(),
      this.api().getMeasureDevices(),
    ]);
    this.state.files = files.status === "fulfilled" ? files.value : [];
    this.state.plotCollection = plots.status === "fulfilled" ? plots.value : emptyPlots(["Plots could not be loaded."]);
    if (calibration.status === "fulfilled") this.state.dummyLoadCalibration = calibration.value;
    if (auth.status === "fulfilled") this.state.contributionAuth = auth.value;
    if (contribution.status === "fulfilled") {
      this.state.contributionDraft = contribution.value;
      this.state.contributionPreview = undefined;
      this.state.contributionError = "";
      this.state.contributionErrorField = undefined;
    } else {
      this.state.contributionDraft = undefined;
      this.state.contributionPreview = undefined;
    }
    if (contributionStatus.status === "fulfilled") this.restoreContributionStatus(contributionStatus.value);
    this.state.manufacturers = manufacturers.status === "fulfilled" ? manufacturers.value.manufacturers : [];
    this.state.deviceSpecificationFields =
      deviceSpecifications.status === "fulfilled" ? deviceSpecifications.value.device_types : {};
    this.state.measureDevices = measureDevices.status === "fulfilled" ? measureDevices.value.devices : [];
    this.state.measureDevicesError = measureDevices.status === "rejected" ? message(measureDevices.reason) : "";
  }

  /** Recover persisted contribution progress (e.g. after a reload or dropped connection mid-submit). */
  private restoreContributionStatus(status: ContributionStatus): void {
    if (!status.session_id || status.session_id !== this.state.snapshot?.session_id) return;
    if (status.state === "preview_ready" && status.preview) {
      this.state.contributionPreview = status.preview;
      return;
    }
    if (status.state === "submitted" && status.submission_url && !this.state.contributionResult) {
      if (status.preview) this.state.contributionPreview = status.preview;
      this.state.contributionResult = {
        status: "success",
        pull_request_url: status.submission_url,
        message: status.message ?? "Contribution submitted",
      };
      return;
    }
    if (status.state === "failed" && status.error && !this.state.contributionResult) {
      if (status.preview) this.state.contributionPreview = status.preview;
      this.state.contributionError = status.error;
      this.state.contributionErrorField = undefined;
    }
  }

  private currentHistoryLocation(): HistoryLocation {
    return locationFromApp(this.state.view, this.state.snapshot?.session_id, this.state.settingsSection);
  }

  private syncHistory(): void {
    const next = this.currentHistoryLocation();
    if (this.applyingHistory) {
      this.lastHistory = next;
      return;
    }
    const mode = historyMode(this.lastHistory, next);
    if (mode === "skip") return;
    const url = hashFor(next);
    if (mode === "replace") this.history.replaceState(next, url);
    else this.history.pushState(next, url);
    this.lastHistory = next;
  }

  private async handlePopState(location: HistoryLocation): Promise<void> {
    this.applyingHistory = true;
    try {
      await this.applyHistoryLocation(location);
    } finally {
      this.applyingHistory = false;
      this.lastHistory = this.currentHistoryLocation();
      this.emitChange();
    }
  }

  private async applyHistoryLocation(location: HistoryLocation): Promise<void> {
    if (this.state.view === "settings" && location.view !== "settings") {
      this.leaveSettings(location.view);
    }
    if (location.view === "settings") {
      this.openSettings(location.settingsSection);
      return;
    }
    if (location.view === "sessions") {
      await this.showSessions();
      return;
    }
    if (location.view === "setup") {
      this.backToSetup();
      return;
    }
    if (location.view === "review") {
      if (this.state.request) this.state.view = "review";
      else this.backToSetup();
      return;
    }
    if (location.sessionId && this.state.snapshot?.session_id !== location.sessionId) {
      await this.openSession(location.sessionId);
    }
    if (location.view === "profile") this.openProfile();
    else if (location.view === "submit") this.openSubmit();
    else if (location.view === "running" && this.state.snapshot && isActive(this.state.snapshot.state)) {
      this.state.view = "running";
      this.connectEvents();
    } else if (location.view === "result" || location.view === "running") {
      if (this.state.snapshot && isTerminal(this.state.snapshot.state)) this.state.view = "result";
    }
  }

  private leaveSettings(nextView: AppView): void {
    this.clearError();
    this.state.view = nextView;
    if (nextView === "setup" || nextView === "review") void this.ensureSetupMeterPreview();
    else this.stopMeterPreview();
    this.changed();
  }
}

const ACTIVE_STATES: ReadonlySet<SessionState> = new Set([
  "running",
  "awaiting_confirmation",
  "cancelling",
  "validating",
  "ready",
]);
const TERMINAL_STATES: ReadonlySet<SessionState> = new Set(["completed", "failed", "cancelled", "resumable"]);

function isActive(state: SessionState): boolean {
  return ACTIVE_STATES.has(state);
}

function isTerminal(state: SessionState): boolean {
  return TERMINAL_STATES.has(state);
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Try again.";
}

function validRetryAfter(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}
