import { LitElement, css, html, nothing } from "lit";
import type { PropertyValues } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import { guard } from "lit/directives/guard.js";
import type {
  ContributionAuthState,
  ContributionDraft,
  ContributionDraftFile,
  ContributionFormValues,
  ContributionPreview,
  ContributionPreviewRequest,
  ContributionResult,
  ContributionSubmitRequest,
  DeviceSpecificationField,
  ErrorHelp,
  MergeModePreview,
  MergePreview,
  PlotCollection,
  PlotHoldDetail,
  PlotYViewDetail,
  SessionFile,
  SessionSnapshot,
  SessionState,
  SessionSummary,
  SettingsSection,
} from "../../types";
import { emit } from "../../utils/events";
import { fileSize, words } from "../../utils/format";
import {
  densityPlotFor,
  plotPairOf,
  reuseYRange,
  sharedYForPlot,
  type PlotPair,
  type PlotYRange,
} from "./interaction";
import { formList, formText, submittedForm } from "../../utils/form";
import { metadataLabels, validateMetadata } from "../profile/validation";
import { diagnosticsDownload, sharedStyles } from "../../styles";
import { errorHelpLink } from "../shared/error-help-link";
import "./plot";
import "../shared/combobox";
import "../shared/sweep-map";
import "../shared/string-list-input";

const CONTRIBUTION_GUIDE_URL =
  "https://docs.powercalc.nl/contributing/measure/output/";
const TROUBLESHOOTING_URL =
  "https://docs.powercalc.nl/contributing/measure/troubleshooting/";
const ZERO_READING_ERROR_PREFIX =
  "Aborting measurement session after repeated 0 W readings.";
const INSPECTABLE_JSON_FILES = new Set(["analyser.json", "analysis.json", "model.json"]);
const ANALYSIS_SUMMARY_LABELS = new Set([
  "Recording analysis",
  "Recording analysis reason",
  "Profile analysis",
  "Profile analysis reason",
  "Analysed feature",
  "Validation MAE",
  "Validation coverage",
]);
const PROFILE_LIBRARY_PATH = "profile_library/<manufacturer>/<model>/";

// Contribution methods. Add a new entry here (e.g. "local") to expose another way to
// contribute; renderMethodPanel routes the selected id to its panel renderer.
type ContributionMethodId = "github" | "manual" | "local";

interface ContributionMethod {
  id: ContributionMethodId;
  title: string;
  summary: string;
  available: boolean;
  unavailableReason?: string;
}

interface ResultOutcome {
  mark: string;
  title: string;
  description: string;
}

const COMPLETED: ResultOutcome = {
  mark: "✓",
  title: "Profile captured",
  description: "The complete output is ready to inspect or download.",
};

const COMPLETED_WITH_READOUT: ResultOutcome = {
  mark: "✓",
  title: "Measurement complete",
  description: "Here is the measured result.",
};

const STOPPED: ResultOutcome = {
  mark: "↻",
  title: "Measurement stopped",
  description: "Any complete output rows have been kept safely.",
};

/** Every terminal state a session can be shown in. Non-terminal states never reach this view. */
const OUTCOMES: Partial<Record<SessionState, ResultOutcome>> = {
  completed: COMPLETED,
  failed: {
    mark: "!",
    title: "Measurement failed",
    description:
      "Review the guidance below and correct the problem before continuing.",
  },
  resumable: {
    mark: "↻",
    title: "A measurement can be resumed",
    description:
      "Compatible output was found. Continue from the last complete variation.",
  },
  cancelled: STOPPED,
};

@customElement("measure-result-view")
export class ResultView extends LitElement {
  @property({ attribute: false })
  snapshot!: SessionSnapshot;

  @property({ attribute: false })
  files: SessionFile[] = [];

  @property({ attribute: false })
  plotCollection: PlotCollection = { partial: false, plots: [], warnings: [] };

  @property({ attribute: false })
  fileUrl: (name: string) => string = () => "";

  @property({ attribute: false })
  downloadAll: () => void = () => {};

  @property({ attribute: false })
  preparedProfileUrl: (jobId: string) => string = () => "";

  @property({ type: String })
  diagnosticsUrl = "";

  @property({ type: Boolean })
  busy = false;

  @property({ type: Boolean })
  canResume = false;

  @property({ type: Boolean })
  canAnalyse = false;

  @property({ type: Boolean })
  analysisComplete = false;

  @property({ attribute: false })
  inspectJsonFile: (name: string) => Promise<unknown> = async () => undefined;

  @property({ type: Boolean })
  canPrepareProfile = true;

  @property({ type: Boolean })
  canMerge = false;

  @property({ type: Boolean })
  canRefine = false;

  @property({ type: Boolean })
  measurementActive = false;

  @property({ attribute: false })
  sessions: SessionSummary[] = [];

  @property({ attribute: false })
  previewMerge?: (rightId: string) => Promise<MergePreview>;

  @property({ attribute: false })
  confirmMerge?: (rightId: string) => Promise<void>;

  @property({ type: Boolean })
  profileMode = false;

  @property({ type: Boolean })
  submitMode = false;

  @property({ type: String })
  errorMessage = "";

  @property({ attribute: false })
  errorHelp?: ErrorHelp;

  @property({ attribute: false })
  contributionAuth?: ContributionAuthState;

  @property({ attribute: false })
  contributionDraft?: ContributionPreview;

  @property({ attribute: false })
  contributionFormValues: ContributionFormValues = {};

  @property({ attribute: false })
  contributionPreview?: ContributionPreview;

  @property({ attribute: false })
  contributionResult?: ContributionResult;

  @property({ type: Boolean })
  contributionBusy = false;

  @property({ type: String })
  contributionError = "";

  @property({ type: String })
  contributionErrorField?: string;

  @property({ attribute: false })
  manufacturers: string[] = [];

  @property({ attribute: false })
  measureDevices: string[] = [];

  @property({ type: Boolean })
  measureDevicesLoading = false;

  @property({ type: String })
  measureDevicesError = "";

  @property({ attribute: false })
  deviceSpecificationFields: Record<string, DeviceSpecificationField[]> = {};

  @state()
  contributionMethod?: ContributionMethodId;

  @state()
  contributionEdit?: ContributionPreviewRequest;

  @state()
  fieldErrors: Record<string, string> = {};

  @state()
  previewDirty = false;

  @state()
  mergeOpen = false;

  @state()
  mergeSelected?: string;

  @state()
  mergePreview?: MergePreview;

  @state()
  mergeBusy = false;

  @state()
  private jsonInspector?: { name: string; content?: unknown; error?: string };

  private inspectorTrigger?: HTMLElement;

  @state()
  mergeError = "";

  @state()
  private linkedHue: number | null = null;

  @state()
  private linkedBri: number | null = null;

  @state()
  private linkedMired: number | null = null;

  @state()
  private heldPointId: string | null = null;

  @state()
  private heldSourcePlotId: string | null = null;

  @state()
  private heldPair: PlotPair | null = null;

  @state()
  private hsLinkedY: PlotYRange | null = null;

  @state()
  private ctLinkedY: PlotYRange | null = null;

  private dismissedServerField?: string;
  private readonly sharedYByPlot = new Map<string, PlotYRange | null>();

  protected willUpdate(changed: PropertyValues<this>): void {
    if (
      changed.has("snapshot") &&
      changed.get("snapshot")?.session_id !== this.snapshot?.session_id
    ) {
      if (this.hasUpdated) this.contributionFormValues = {};
      this.contributionEdit = undefined;
      this.fieldErrors = {};
      this.previewDirty = false;
      this.dismissedServerField = undefined;
      this.mergeOpen = false;
      this.mergeSelected = undefined;
      this.mergePreview = undefined;
      this.mergeError = "";
      this.linkedHue = null;
      this.linkedBri = null;
      this.linkedMired = null;
      this.hsLinkedY = null;
      this.ctLinkedY = null;
      this.clearPlotHold();
    }
    if (changed.has("contributionPreview") && this.contributionPreview) {
      if (this.hasUpdated) this.contributionFormValues = {};
      this.contributionEdit = undefined;
      this.previewDirty = false;
      this.fieldErrors = {};
    }
    this.previewDirty =
      Object.keys(this.contributionFormValues).length > 0 || this.previewDirty;
    if (changed.has("contributionBusy") && this.contributionBusy)
      this.dismissedServerField = undefined;
  }

  protected updated(changed: PropertyValues<this>): void {
    if (
      changed.has("contributionError") &&
      this.contributionError &&
      !this.submitMode
    ) {
      this.focusValidation();
    }
    const dialog = this.shadowRoot?.querySelector<HTMLDialogElement>(".json-dialog");
    if (dialog && !dialog.open) dialog.showModal();
  }

  static readonly styles = [
    sharedStyles,
    css`
      .result-summary {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr);
        gap: 1rem;
        align-items: start;
        padding-bottom: 1.5rem;
        border-bottom: 1px solid var(--line);
      }
      .result-summary h2 {
        margin-bottom: 0.4rem;
      }
      .result-summary .muted {
        margin-bottom: 0;
      }
      .status-mark {
        display: grid;
        place-items: center;
        width: 48px;
        height: 48px;
        border: 1px solid var(--line);
        border-radius: 50%;
        color: var(--good);
        font:
          700 1.3rem/1 ui-monospace,
          monospace;
      }
      .status-mark.failed {
        color: var(--danger);
      }
      .status-mark.cancelled,
      .status-mark.resumable {
        color: var(--signal);
      }
      .readout {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 1px;
        overflow: hidden;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: var(--line);
        margin-top: 1.5rem;
      }
      .metric {
        padding: 1rem;
        background: var(--field);
      }
      .metric span {
        display: block;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.1em;
      }
      .metric strong {
        display: block;
        margin-top: 0.35rem;
        font:
          700 1.4rem/1.1 "DIN Alternate",
          sans-serif;
        color: var(--signal-strong);
      }
      .files-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        margin-top: 1.5rem;
      }
      .files-header h3 {
        margin: 0;
        font-size: 1rem;
      }
      .download-all {
        min-height: 36px;
        padding: 0.45rem 0.75rem;
        border-radius: 999px;
        font-size: 0.72rem;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }
      .plots-header {
        margin: 1.5rem 0 0.75rem;
      }
      .plots-header h3 {
        margin: 0;
        font-size: 1rem;
      }
      .plots {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr));
        gap: 1rem;
      }
      .plot-warning {
        margin-top: 0.75rem;
      }
      .contribution {
        margin-top: 1.5rem;
        padding: clamp(1rem, 3vw, 1.35rem);
        border: 1px solid color-mix(in srgb, var(--signal) 48%, var(--line));
        border-radius: 14px;
        background: color-mix(in srgb, var(--signal) 7%, var(--well));
      }
      .profile-metadata {
        padding: 0;
        border: 0;
        border-radius: 0;
        background: transparent;
      }
      .profile-guidance {
        max-width: 1000px;
        line-height: 1.55;
      }
      .contribution h3 {
        margin: 0 0 0.35rem;
        font-size: 1.15rem;
      }
      .contribution > p.muted {
        margin: 0;
        color: var(--muted);
      }
      .delivery-methods {
        margin-top: 1.25rem;
        padding-top: 1.25rem;
        border-top: 1px solid var(--line);
      }
      .delivery-methods > p.muted {
        margin: 0;
      }
      .contribution-methods {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
        gap: 0.75rem;
        margin: 1.1rem 0;
      }
      .method-card {
        display: grid;
        gap: 0.35rem;
        padding: 0.85rem 0.95rem;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: var(--well);
        text-align: left;
        cursor: pointer;
      }
      .method-card:hover:not(:disabled) {
        border-color: var(--signal);
      }
      .method-card.active {
        border-color: var(--signal);
        background: color-mix(in srgb, var(--signal) 12%, var(--well));
        box-shadow: inset 0 0 0 1px var(--signal);
      }
      .method-card:disabled {
        cursor: default;
        opacity: 0.55;
      }
      .method-card strong {
        color: var(--ink);
        font-size: 0.95rem;
      }
      .method-card span {
        color: var(--muted);
        font-size: 0.8rem;
      }
      .method-flag {
        color: var(--signal-strong);
        font-size: 0.7rem;
        font-style: normal;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      .contribution-next ol {
        margin: 0 0 1rem;
        padding-left: 1.4rem;
        color: var(--ink);
      }
      .contribution-next li {
        display: list-item;
        padding: 0.25rem 0 0.25rem 0.2rem;
        border: 0;
      }
      .contribution-next code {
        color: var(--signal-strong);
        font-size: 0.88em;
        overflow-wrap: anywhere;
      }
      .contribution-guide {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        min-height: 40px;
        padding: 0.55rem 0.8rem;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--surface-raised);
        text-decoration: none;
      }
      .contribution-guide:hover {
        border-color: var(--signal);
      }
      .contribution-auto {
        padding: clamp(0.85rem, 3vw, 1.2rem);
        border: 1px solid var(--line);
        border-radius: 12px;
        background: color-mix(in srgb, var(--field) 68%, transparent);
      }
      .contribution-form {
        display: grid;
      }
      .validation-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1.25rem;
        margin-top: 1.5rem;
        padding: 1rem 1.1rem;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: color-mix(in srgb, var(--field) 72%, transparent);
      }
      .validation-status {
        margin: 0;
        color: var(--muted);
        line-height: 1.45;
      }
      .validation-status.pending {
        color: var(--ink);
      }
      .validation-status.valid {
        color: var(--good);
        font-weight: 650;
      }
      .validation-footer button {
        flex: 0 0 auto;
      }
      .validation-summary {
        margin: 0 0 1rem;
      }
      .validation-summary ul {
        display: block;
        margin: 0.5rem 0 0;
        padding-left: 1.25rem;
      }
      .validation-summary li {
        display: list-item;
        padding: 0.15rem 0;
        border: 0;
      }
      .validation-summary button {
        min-height: 0;
        padding: 0;
        border: 0;
        background: transparent;
        color: inherit;
        text-align: left;
        text-decoration: underline;
        font: inherit;
      }
      .required-guidance {
        margin: 0 0 1rem;
        font-size: 0.8rem;
      }
      .metadata-group {
        min-inline-size: 0;
        margin: 0;
        padding: 0;
        border: 0;
        border-top: 1px solid var(--line);
      }
      .metadata-group legend {
        padding: 0 0.65rem 0 0;
        color: var(--ink);
        font-size: 1rem;
        font-weight: 700;
      }
      .metadata-group-body {
        display: grid;
        gap: 0.8rem;
        padding: 0.75rem 0 1.5rem;
      }
      .profile-details {
        min-width: 0;
      }
      .profile-details summary {
        padding: 0.6rem 0;
        color: var(--muted);
        cursor: pointer;
        font-size: 0.82rem;
      }
      .profile-details summary:hover {
        color: var(--ink);
      }
      .profile-details-body {
        display: grid;
        gap: 1rem;
        padding: 0.5rem 0;
      }
      .prepared-preview {
        margin-top: 1rem;
      }
      .preparation-warning {
        margin: 1rem 0 0;
      }
      .metadata-group-description {
        margin: 0;
        color: var(--muted);
        font-size: 0.8rem;
      }
      .contribution-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.8rem;
        align-items: start;
      }
      .contribution-grid.contributor-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
      .contribution-grid > *,
      .contribution-grid label,
      .notes-field {
        align-self: start;
      }
      .contribution-grid label,
      .notes-field {
        display: grid;
        gap: 0.4rem;
      }
      .contribution-grid label > span,
      .notes-field > span {
        color: var(--muted);
        font-size: 0.82rem;
        font-weight: 650;
      }
      .field-stack {
        display: grid;
        gap: 0.4rem;
        min-width: 0;
      }
      .preview-block span,
      .info-list span {
        color: var(--muted);
        font-size: 0.76rem;
        font-weight: 650;
      }
      textarea {
        min-height: 84px;
        resize: vertical;
      }
      .info-list {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
        gap: 0.5rem 0.8rem;
        margin: 0;
      }
      .info-list div {
        min-width: 0;
      }
      .info-list dd {
        margin: 0.15rem 0 0;
        overflow-wrap: anywhere;
      }
      .preview-block {
        display: grid;
        gap: 0.45rem;
        min-width: 0;
      }
      .schema-valid {
        border-left-color: var(--good);
        background: color-mix(in srgb, var(--good) 9%, transparent);
      }
      pre {
        max-height: 240px;
        overflow: auto;
        margin: 0;
        padding: 0.8rem;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--well);
        color: var(--ink);
        font-size: 0.75rem;
        line-height: 1.45;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      .confirm-row {
        display: flex;
        align-items: flex-start;
        gap: 0.65rem;
        margin-top: 0.75rem;
        min-height: 44px;
        color: var(--muted);
        font-size: 0.82rem;
        line-height: 1.5;
        cursor: pointer;
      }
      .confirm-row input {
        flex: 0 0 1rem;
        width: 1rem;
        height: 1rem;
        min-height: 0;
        margin: 0.1rem 0 0;
        padding: 0;
        accent-color: var(--signal);
        cursor: pointer;
      }
      .confirm-row > span {
        min-width: 0;
        font-weight: 400;
      }
      .success-link {
        display: inline-flex;
        margin-top: 0.75rem;
        color: var(--good);
        font-weight: 700;
      }
      .manufacturer-library-link {
        white-space: nowrap;
      }
      .merge {
        margin-top: 1.15rem;
      }
      .merge h3 {
        margin: 0 0 0.35rem;
      }
      .merge-picker {
        display: grid;
        gap: 0.75rem;
        margin-top: 1rem;
      }
      .merge-group {
        display: grid;
        gap: 0.45rem;
      }
      .merge-group h4 {
        margin: 0;
        color: var(--muted);
        font-size: 0.76rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      .merge-option {
        display: grid;
        gap: 0.15rem;
        padding: 0.7rem 0.85rem;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--well);
        text-align: left;
      }
      .merge-option.active {
        border-color: var(--signal);
        box-shadow: inset 0 0 0 1px var(--signal);
      }
      .merge-option strong {
        color: var(--ink);
      }
      .merge-option .muted {
        display: block;
      }
      .merge-preview {
        margin: 0;
        color: var(--ink);
        line-height: 1.5;
      }
      .auth-shortcut {
        display: flex;
        flex-wrap: wrap;
        gap: 0.75rem;
        align-items: center;
        justify-content: space-between;
        padding: 0.8rem;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--well);
      }
      ul {
        list-style: none;
        margin: 0.65rem 0 0;
        padding: 0;
        border-top: 1px solid var(--line);
      }
      li {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto auto;
        align-items: center;
        gap: 1rem;
        padding: 0.8rem 0;
        border-bottom: 1px solid var(--line);
      }
      li span {
        overflow-wrap: anywhere;
      }
      li small {
        color: var(--muted);
      }
      a {
        color: var(--signal-strong);
        font-weight: 700;
      }
      @media (max-width: 520px) {
        .files-header {
          align-items: flex-start;
          flex-direction: column;
        }
        li {
          grid-template-columns: 1fr auto;
        }
        li small {
          grid-column: 1;
          grid-row: 2;
        }
        .contribution-grid,
        .contribution-grid.contributor-grid {
          grid-template-columns: 1fr;
        }
        .validation-footer {
          align-items: stretch;
          flex-direction: column;
        }
        .validation-footer button {
          width: 100%;
        }
      }
    `,
  ];

  render() {
    const state = this.snapshot.state;
    const outcome = this.outcome(state);
    const recordedError =
      typeof this.snapshot.error === "string"
        ? this.snapshot.error
        : this.snapshot.error?.message;
    const error =
      recordedError ||
      (state === "failed"
        ? "The measurement ended without finishing, and no error was recorded."
        : "");
    if (this.profileMode) {
      if (this.submitMode) {
        return html` <section class="panel" aria-labelledby="submit-title">
          <p class="eyebrow">06 / Submit profile</p>
          <h2 id="submit-title">Choose how to submit the profile</h2>
          <p class="muted">
            The enriched profile is validated and ready. Choose where it should
            go.
          </p>
          ${this.renderDeliverySection(state)}
          <div class="actions">
            <button type="button" @click=${() => this.emit("back")}>
              Back to preparation
            </button>
          </div>
        </section>`;
      }
      return html` <section class="panel" aria-labelledby="profile-title">
        <p class="eyebrow">05 / Prepare</p>
        <h2 id="profile-title">Prepare your Powercalc profile</h2>
        <p class="muted profile-guidance">
          Review and enrich the metadata added to model.json.
          ${(this.contributionPreview?.manufacturer_library_url ??
          this.contributionDraft?.manufacturer_library_url)
            ? html`Check the
                <a
                  class="manufacturer-library-link"
                  href=${this.contributionPreview?.manufacturer_library_url ??
                  this.contributionDraft?.manufacturer_library_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  >existing manufacturer profiles
                  <span aria-hidden="true">↗</span></a
                >
                and match the naming and metadata patterns used there.`
            : nothing}
        </p>
        ${this.renderPreparationSection(state)}
        <div class="actions">
          <button type="button" @click=${() => this.emit("back")}>
            Back to result
          </button>
        </div>
      </section>`;
    }
    return html`
      <section class="panel" aria-labelledby="result-title">
        <p class="eyebrow">04 / Result</p>
        <div class="result-summary">
          <div class="status-mark ${state}" aria-hidden="true">
            ${outcome.mark}
          </div>
          <div>
            <h2 id="result-title">${outcome.title}</h2>
            <p class="muted">${outcome.description}</p>
          </div>
        </div>
        ${error
          ? html`<p class="notice error" role="alert">
              ${this.renderError(error)}
            </p>`
          : nothing}
        ${state !== "completed" && this.files.length ? html`<p class="notice" role="status">
          Saved output is available below. This measurement is incomplete; these files are partial results.
          ${this.canResume ? "Resume to continue from the last complete variation." : "You can download the saved data before starting again."}
        </p>` : nothing}
        ${this.renderSummary()} ${this.renderAnalysis()} ${this.renderWarnings()} ${this.renderSweepMap()} ${this.renderPlots()}
        ${this.renderFiles()} ${this.renderJsonInspector()} ${this.renderNextActions(state)}
        ${this.errorMessage
          ? html`<p class="notice error" role="alert">
              ${this.errorMessage}${errorHelpLink(this.errorHelp)}
            </p>`
          : nothing}
        ${diagnosticsDownload(this.diagnosticsUrl)}
        <div class="actions">
          <button type="button" @click=${() => this.emit("sessions")}>
            Measurements
          </button>
          <button type="button" @click=${() => this.emit("new")}>
            New measurement
          </button>
          ${this.renderRefine()} ${this.renderResume(state)}
        </div>
      </section>
    `;
  }

  private renderSweepMap() {
    return html`<measure-sweep-map
      .coverage=${this.snapshot.sweep_coverage}
    ></measure-sweep-map>`;
  }

  private renderNextActions(state: SessionState) {
    const showPrepare = state === "completed" && this.canPrepareProfile;
    const showMerge = this.canMerge;
    if (!showPrepare && !showMerge) return nothing;
    return html` <section
      class="contribution"
      aria-labelledby="whats-next-title"
    >
      <p class="eyebrow">What's next?</p>
      ${showPrepare
        ? html`
            <h3 id="whats-next-title">Prepare the profile</h3>
            <p class="muted">
              Add product and measurement metadata, validate the result, and
              then download it or open a pull request.
            </p>
            <div class="actions">
              <button
                class="primary"
                type="button"
                @click=${() => this.emit("prepare")}
              >
                Prepare profile
              </button>
            </div>
          `
        : html`<h3 id="whats-next-title">Combine measurements</h3>`}
      ${showMerge ? this.renderMerge() : nothing}
    </section>`;
  }

  private renderMerge() {
    const currentId = this.snapshot.session_id ?? "";
    const currentModel = this.snapshot.request?.model_id ?? "";
    const candidates = this.sessions.filter(
      (session) => session.session_id !== currentId && session.can_merge,
    );
    const sameModel = candidates.filter(
      (session) => session.model_id === currentModel,
    );
    const otherModels = candidates.filter(
      (session) => session.model_id !== currentModel,
    );
    return html`
      <div class="merge">
        <h3>Merge with another session</h3>
        <p class="muted">
          Union LUT points by key. Quality rank decides clashes. This does not
          stop a running measurement.
        </p>
        <div class="actions">
          <button
            type="button"
            @click=${this.toggleMergePicker}
            ?disabled=${this.mergeBusy || !candidates.length}
          >
            ${this.mergeOpen ? "Hide sessions" : "Merge with another session"}
          </button>
        </div>
        ${!candidates.length
          ? html`<p class="muted">
              No other sessions with LUT points are available to merge.
            </p>`
          : nothing}
        ${this.mergeOpen
          ? this.renderMergePicker(sameModel, otherModels)
          : nothing}
      </div>
    `;
  }

  private renderMergePicker(
    sameModel: SessionSummary[],
    otherModels: SessionSummary[],
  ) {
    return html`
      <div class="merge-picker">
        ${sameModel.length
          ? html`<div class="merge-group">
              <h4>Same model</h4>
              ${sameModel.map((session) =>
                this.renderMergeOption(session, false),
              )}
            </div>`
          : nothing}
        ${otherModels.length
          ? html`<div class="merge-group">
              <h4>Other models</h4>
              <p class="notice warning">
                These sessions have a different model ID.
              </p>
              ${otherModels.map((session) =>
                this.renderMergeOption(session, true),
              )}
            </div>`
          : nothing}
        ${this.mergeError
          ? html`<p class="notice error" role="alert">${this.mergeError}</p>`
          : nothing}
        ${this.mergePreview
          ? this.renderMergePreview(this.mergePreview)
          : nothing}
        ${this.mergePreview
          ? html`<div class="actions">
              <button
                class="primary"
                type="button"
                @click=${this.confirmSelectedMerge}
                ?disabled=${this.mergeBusy}
              >
                ${this.mergeBusy ? "Merging…" : "Create merged session"}
              </button>
            </div>`
          : nothing}
      </div>
    `;
  }

  private renderMergeOption(session: SessionSummary, warn: boolean) {
    const active = this.mergeSelected === session.session_id;
    return html`
      <button
        type="button"
        class="merge-option ${active ? "active" : ""}"
        ?disabled=${this.mergeBusy}
        @click=${() => void this.selectMerge(session.session_id)}
      >
        <strong
          >${session.product_name || session.model_id || "Measurement"}</strong
        >
        <span class="muted"
          >${session.model_id}${warn ? " · different model" : ""}</span
        >
      </button>
    `;
  }

  private renderMergePreview(preview: MergePreview) {
    const lines = Object.entries(preview.modes).map(([mode, stats]) =>
      formatMergeMode(mode, stats),
    );
    return html`
      ${preview.model_id_warning
        ? html`<p class="notice warning">
            The selected sessions have different model IDs.
          </p>`
        : nothing}
      <p class="merge-preview">${lines.join(" · ")}</p>
    `;
  }

  private renderRefine() {
    if (!this.canRefine) return nothing;
    return html`<button
      type="button"
      title="Keep existing points; only measure what’s missing."
      @click=${() => this.emit("refine")}
      ?disabled=${this.busy || this.measurementActive}
    >
      Refine
    </button>`;
  }

  private toggleMergePicker = (): void => {
    this.mergeOpen = !this.mergeOpen;
  };

  private async selectMerge(sessionId: string): Promise<void> {
    this.mergeSelected = sessionId;
    this.mergeError = "";
    this.mergePreview = undefined;
    if (!this.previewMerge) return;
    this.mergeBusy = true;
    try {
      this.mergePreview = await this.previewMerge(sessionId);
    } catch (error) {
      this.mergeError =
        error instanceof Error ? error.message : "Could not preview the merge.";
    } finally {
      this.mergeBusy = false;
    }
  }

  private async confirmSelectedMerge(): Promise<void> {
    if (!this.mergeSelected || !this.confirmMerge) return;
    this.mergeBusy = true;
    this.mergeError = "";
    try {
      await this.confirmMerge(this.mergeSelected);
    } catch (error) {
      this.mergeError =
        error instanceof Error
          ? error.message
          : "Could not create the merged session.";
    } finally {
      this.mergeBusy = false;
    }
  }

  private renderError(error: string) {
    if (!error.startsWith(ZERO_READING_ERROR_PREFIX)) return error;
    return html`
      Aborting measurement session after repeated 0 W readings. The power meter may not resolve this low load.
      Verify the device is on and connected, measure multiple identical lights together, add a resistive dummy load,
      or use a more sensitive meter. See
      <a href=${TROUBLESHOOTING_URL} target="_blank" rel="noopener noreferrer">Troubleshooting guide</a>
      for troubleshooting guidance.
    `;
  }

  private renderFiles() {
    if (this.files.length) {
      return html`
        <div class="files-header">
          <h3>${this.snapshot.state === "completed" ? "Generated files" : "Saved partial files"}</h3>
          <button
            class="download-all"
            type="button"
            @click=${() => this.downloadAll()}
          >
            Download all
          </button>
        </div>
        <ul>
          ${this.files.map(
            (file) => html`
              <li>
                <span>${file.name}</span><small>${fileSize(file.size)}</small>
                <div class="file-actions">
                  ${this.isInspectableJson(file) ? html`
                    <button class="inspect-file" type="button" title=${`View ${file.name}`} aria-label=${`View ${file.name}`} @click=${(event: MouseEvent) => void this.openJsonInspector(file.name, event.currentTarget as HTMLElement)}>
                      View
                    </button>
                  ` : nothing}
                  <a href=${this.fileUrl(file.name)} download
                    >Download<span class="sr-only"> ${file.name}</span></a
                  >
                </div>
              </li>
            `,
          )}
        </ul>
      `;
    }
    if (this.summaryEntries().length) return nothing;
    return html`<p class="notice">
      No downloadable files are available for this session.
    </p>`;
  }

  private onPlotHueLink = (
    event: CustomEvent<{ hue: number | null }>,
  ): void => {
    if (this.heldPair === "hs") return;
    this.linkedHue = event.detail.hue;
  };

  private onPlotBriLink = (
    event: CustomEvent<{ bri: number | null }>,
  ): void => {
    if (this.heldPair === "color_temp") return;
    this.linkedBri = event.detail.bri;
  };

  private onPlotMiredLink = (
    event: CustomEvent<{ mired: number | null }>,
  ): void => {
    if (this.heldPair === "color_temp") return;
    this.linkedMired = event.detail.mired;
  };

  private onPlotYView = (event: CustomEvent<PlotYViewDetail>): void => {
    if (event.detail.pair === "hs") this.hsLinkedY = event.detail.y;
    else this.ctLinkedY = event.detail.y;
  };

  private linkedYFor(plotId: string): PlotYRange | null {
    const pair = plotPairOf(plotId);
    if (pair === "hs") return this.hsLinkedY;
    if (pair === "color_temp") return this.ctLinkedY;
    return null;
  }

  private stableSharedY(plotId: string): PlotYRange | null {
    const next = reuseYRange(
      this.sharedYByPlot.get(plotId),
      sharedYForPlot(this.plotCollection.plots, plotId),
    );
    this.sharedYByPlot.set(plotId, next);
    return next;
  }

  private onPlotHold = (event: CustomEvent<PlotHoldDetail | null>): void => {
    const detail = event.detail;
    if (!detail) {
      this.clearPlotHold();
      return;
    }
    const pair = plotPairOf(detail.plotId);
    this.heldPointId = detail.pointId;
    this.heldSourcePlotId = detail.plotId;
    this.heldPair = pair;
    if (pair === "hs") this.linkedHue = detail.hue;
    if (pair === "color_temp") {
      this.linkedBri = detail.bri;
      this.linkedMired = detail.mired;
    }
  };

  private clearPlotHold(): void {
    this.heldPointId = null;
    this.heldSourcePlotId = null;
    this.heldPair = null;
    this.linkedHue = null;
    this.linkedBri = null;
    this.linkedMired = null;
  };

  private renderPlots() {
    const { plots, warnings, partial } = this.plotCollection;
    if (!plots.length && !warnings.length) return nothing;
    return html`
      ${plots.length
        ? html`
            <div class="plots-header"><h3>Result plots</h3></div>
            <div class="plots">
              ${plots.map(
                (plot) =>
                  html`<measure-result-plot
                    .plot=${plot}
                    .partial=${partial}
                    .editable=${Boolean(this.plotCollection.editable)}
                    .sharedY=${this.stableSharedY(plot.id)}
                    .linkedY=${this.linkedYFor(plot.id)}
                    .heldPointId=${this.heldPointId}
                    .heldSourcePlotId=${this.heldSourcePlotId}
                    .linksLocked=${plotPairOf(plot.id) === this.heldPair}
                    .filterHue=${plot.id === "hs" ? this.linkedHue : null}
                    .filterBri=${plot.id === "color_temp_max_bri"
                      ? this.linkedBri
                      : null}
                    .filterMired=${plot.id === "color_temp"
                      ? this.linkedMired
                      : null}
                    .densityPlot=${densityPlotFor(plots, plot)}
                    @plot-hue-link=${this.onPlotHueLink}
                    @plot-bri-link=${this.onPlotBriLink}
                    @plot-mired-link=${this.onPlotMiredLink}
                    @plot-hold=${this.onPlotHold}
                    @plot-y-view=${this.onPlotYView}
                  ></measure-result-plot>`,
              )}
            </div>
          `
        : nothing}
      ${warnings.map(
        (warning) => html`<p class="notice plot-warning">${warning}</p>`,
      )}
    `;
  }

  private contributionMethods(): ContributionMethod[] {
    const draft = this.contributionPreview ?? this.contributionDraft;
    return [
      {
        id: "github",
        title: "GitHub pull request",
        summary:
          "Open a pull request to the shared Powercalc profile library, straight from here.",
        available: Boolean(draft?.eligible),
        unavailableReason:
          draft?.reason ??
          "This session is not eligible for automatic contribution.",
      },
      {
        id: "manual",
        title: "Manual contribution",
        summary:
          "Download the generated files and open the pull request yourself.",
        available: true,
      },
      {
        id: "local",
        title: "Add to this installation",
        summary:
          "Use the measured profile directly in your local Powercalc setup.",
        available: false,
        unavailableReason: "Coming soon.",
      },
    ];
  }

  private selectedMethod(methods: ContributionMethod[]): ContributionMethodId {
    const chosen = methods.find(
      (method) => method.id === this.contributionMethod && method.available,
    );
    if (chosen) return chosen.id;
    return methods.find((method) => method.available)?.id ?? "manual";
  }

  private renderPreparationSection(state: SessionSnapshot["state"]) {
    if (state !== "completed") return nothing;
    return html`
      <section class="contribution profile-metadata">
        ${this.renderPreparationPanel()}
      </section>
    `;
  }

  private renderDeliverySection(state: SessionSnapshot["state"]) {
    if (state !== "completed") return nothing;
    const methods = this.contributionMethods();
    const selected = this.selectedMethod(methods);
    return html` <section
      class="contribution profile-delivery"
      aria-labelledby="delivery-title"
    >
      <h3 id="delivery-title">Available options</h3>
      <p class="muted">
        You can return to preparation without losing the validated metadata.
      </p>
      <div
        class="contribution-methods"
        role="radiogroup"
        aria-label="Profile delivery method"
      >
        ${methods.map((method) => this.renderMethodCard(method, selected))}
      </div>
      ${this.renderMethodPanel(selected)}
    </section>`;
  }

  private renderMethodCard(
    method: ContributionMethod,
    selected: ContributionMethodId,
  ) {
    const active = method.id === selected;
    return html`
      <button
        type="button"
        role="radio"
        aria-checked=${active ? "true" : "false"}
        class="method-card ${active ? "active" : ""}"
        ?disabled=${!method.available}
        @click=${() => this.selectMethod(method.id)}
      >
        <strong>${method.title}</strong>
        <span>${method.summary}</span>
        ${method.available
          ? nothing
          : html`<em class="method-flag">${method.unavailableReason}</em>`}
      </button>
    `;
  }

  private selectMethod(id: ContributionMethodId): void {
    this.contributionMethod = id;
  }

  private renderMethodPanel(method: ContributionMethodId) {
    if (method === "github") return this.renderGithubPanel();
    if (method === "local") return this.renderLocalPanel();
    return this.renderManualPanel();
  }

  private renderManualPanel() {
    const preview = this.contributionPreview;
    const downloadUrl = preview?.job_id
      ? this.preparedProfileUrl(preview.job_id)
      : "";
    return html`
      <div class="contribution-next">
        <ol>
          <li>
            ${preview
              ? "The enriched profile is validated and ready."
              : "Validate the profile metadata before downloading it."}
          </li>
          <li>
            ${downloadUrl
              ? html`<a href=${downloadUrl} download="powercalc-profile.zip"
                    >Download the prepared profile ZIP</a
                  >.`
              : "A prepared ZIP becomes available after validation."}
          </li>
          <li>
            Extract it into a Powercalc checkout; it already contains
            <code>${PROFILE_LIBRARY_PATH}</code>.
          </li>
          <li>Open a pull request using the power profile template.</li>
        </ol>
        <a
          class="contribution-guide"
          href=${CONTRIBUTION_GUIDE_URL}
          target="_blank"
          rel="noopener noreferrer"
        >
          Read the contribution guide <span aria-hidden="true">↗</span>
        </a>
      </div>
    `;
  }

  private renderLocalPanel() {
    return html`
      <div class="contribution-local">
        <p class="muted">
          Adding a measured profile directly to your local Powercalc
          installation is coming soon.
        </p>
      </div>
    `;
  }

  private renderGithubPanel() {
    const draft = this.contributionPreview ?? this.contributionDraft;
    if (!draft?.eligible) {
      return html`<div class="contribution-auto">
        <p class="muted">
          ${draft?.reason ??
          "This session is not eligible for automatic contribution."}
        </p>
      </div>`;
    }
    return html`
      <div class="contribution-auto">
        ${this.renderContributionAuthShortcut()}
        ${this.contributionError
          ? html`<p class="notice error" role="alert">
              ${this.contributionError}
            </p>`
          : nothing}
        ${this.contributionPreview
          ? this.renderGithubPreview(this.contributionPreview)
          : html`<p class="muted">
              Refresh the profile preview above before opening a pull request.
            </p>`}
        ${this.renderContributionResult()}
      </div>
    `;
  }

  private renderPreparationPanel() {
    const draft = this.editableDraft();
    if (!draft?.eligible) {
      return html`<div class="contribution-auto">
        <p class="muted">
          ${draft?.reason ??
          "This measurement cannot be prepared as a profile."}
        </p>
      </div>`;
    }
    return html`
      <div class="contribution-auto">
        <form
          class="contribution-form"
          novalidate
          @submit=${this.previewContribution}
          @input=${this.metadataChanged}
          @change=${this.metadataChanged}
          @focusout=${this.validateField}
          @combobox-change=${this.metadataChanged}
          @list-input-change=${this.metadataChanged}
        >
          ${this.renderValidationSummary()}
          <p class="muted required-guidance">
            Fields marked
            <span class="required-marker" aria-hidden="true">*</span
            ><span class="sr-only">with an asterisk</span> are required.
          </p>
          ${this.renderProductMetadata(draft)}
          ${this.renderContributorMetadata(draft)}
          ${this.renderMeasurementMetadata(draft)}
          ${this.renderDeviceSpecifications(draft)}
          ${this.renderContributionNotes(draft)}
          ${this.renderMeasurementContext(draft)}
          ${this.renderValidationFooter()}
        </form>
        ${this.contributionPreview && this.canContinue()
          ? this.renderPreparedPreview(this.contributionPreview)
          : nothing}
      </div>
    `;
  }

  private renderProductMetadata(draft: ContributionDraft) {
    const manufacturer = this.fieldValue(
      "manufacturer_name",
      draft.manufacturer_name,
    );
    return html`<fieldset
      class="metadata-group"
      ?disabled=${this.contributionBusy}
    >
      <legend>Product</legend>
      <div class="metadata-group-body">
        <p class="metadata-group-description">
          Identity and manufacturer details used to place and discover this
          profile.
        </p>
        <div class="contribution-grid">
          <div class="field-stack">
            <measure-combobox
              name="manufacturer_name"
              label="Manufacturer"
              .error=${this.fieldError("manufacturer_name")}
              ?disabled=${this.contributionBusy}
              .value=${manufacturer}
              .options=${this.manufacturers.map((name) => ({
                value: name,
                label: name,
              }))}
              placeholder="Search or enter a manufacturer"
              hint="Choose an existing manufacturer or enter a new one."
              required
              allowCustom
            >
              <input
                slot="value"
                type="hidden"
                name="manufacturer_name"
                .value=${manufacturer}
              />
            </measure-combobox>
          </div>
          ${this.input("model_id", "Model ID", draft.model_id, {
            placeholder: "LED2408G10",
            hint: "The identifier from the manufacturer’s product page. A short number from Home Assistant is usually a store SKU — it is added as an alias instead.",
          })}
          ${this.input("product_name", "Product name", draft.product_name, {
            hint: "Use the marketed name without repeating the manufacturer, e.g. “Hue White Ambiance GU10”.",
          })}
          ${this.input(
            "product_url",
            "Manufacturer product URL",
            draft.product_url ?? "",
            {
              required: false,
              placeholder: "https://…",
            },
          )}
          <measure-string-list-input
            name="aliases"
            label="Model aliases"
            itemLabel="Alias"
            .value=${this.fieldList("aliases", draft.aliases ?? [])}
            .error=${this.fieldError("aliases")}
            ?disabled=${this.contributionBusy}
            placeholder="Enter a model alias"
            hint="Add each alternative model identifier separately."
          ></measure-string-list-input>
          <measure-string-list-input
            name="gtins"
            label="GTIN / barcodes"
            itemLabel="Barcode"
            .inputMode=${"numeric"}
            .value=${this.fieldList("gtins", draft.gtins ?? [])}
            .error=${this.fieldError("gtins")}
            ?disabled=${this.contributionBusy}
            placeholder="Enter a barcode"
            hint="Add one 8, 12, 13 or 14 digit barcode per field."
          ></measure-string-list-input>
        </div>
      </div>
    </fieldset>`;
  }

  private renderContributorMetadata(draft: ContributionDraft) {
    return html`<fieldset
      class="metadata-group"
      ?disabled=${this.contributionBusy}
    >
      <legend>Contributor</legend>
      <div class="metadata-group-body">
        <p class="metadata-group-description">
          These details are prefilled from your profile settings and credited in
          model.json.
        </p>
        <div class="contribution-grid contributor-grid">
          ${this.input("contributor", "Name", draft.contributor)}
          ${this.input(
            "contributor_github",
            "GitHub username",
            draft.contributor_github ??
              this.contributionAuth?.identity?.login ??
              "",
          )}
          ${this.input(
            "contributor_email",
            "Email",
            draft.contributor_email ?? "",
            { required: false },
          )}
        </div>
      </div>
    </fieldset>`;
  }

  private renderMeasurementMetadata(draft: ContributionDraft) {
    const measureDevice = this.fieldValue(
      "measure_device",
      draft.measure_device,
    );
    const hint = this.measureDevicesLoading
      ? "Loading names used by existing Powercalc profiles…"
      : "Choose an existing power meter or enter its manufacturer and model.";
    return html`<fieldset
      class="metadata-group"
      ?disabled=${this.contributionBusy}
    >
      <legend>Measurement</legend>
      <div class="metadata-group-body">
        <p class="metadata-group-description">
          Document the equipment and method used to create the profile.
        </p>
        <div class="contribution-grid">
          <div class="field-stack">
            <measure-combobox
              name="measure_device"
              label="Measurement device"
              .value=${measureDevice}
              .options=${this.measureDevices.map((device) => ({
                value: device,
                label: device,
              }))}
              .error=${this.fieldError("measure_device")}
              ?disabled=${this.contributionBusy}
              placeholder="e.g. Shelly Plug S"
              .hint=${hint}
              required
              allowCustom
            >
              <input
                slot="value"
                type="hidden"
                name="measure_device"
                .value=${measureDevice}
              />
            </measure-combobox>
            ${this.measureDevicesError
              ? html`<small class="field-hint error" role="status"
                  >Library suggestions are unavailable; manual entry still
                  works.</small
                >`
              : nothing}
          </div>
          ${this.input(
            "measure_device_firmware",
            "Device firmware",
            draft.measure_device_firmware ?? "",
            {
              required: false,
            },
          )}
          ${this.renderMainsVoltage(draft)}
        </div>
        ${this.renderTextarea(
          "measure_description",
          "Measurement description",
          draft.measure_description,
        )}
      </div>
    </fieldset>`;
  }

  private renderContributionNotes(draft: ContributionDraft) {
    return html`<fieldset
      class="metadata-group"
      ?disabled=${this.contributionBusy}
    >
      <legend>Contribution notes</legend>
      <div class="metadata-group-body">
        <p class="metadata-group-description">
          Optional context for reviewers; this is not added to model.json.
        </p>
        ${this.renderTextarea("notes", "Notes", draft.notes)}
      </div>
    </fieldset>`;
  }

  private renderTextarea(
    name: "measure_description" | "notes",
    label: string,
    value: unknown,
  ) {
    const error = this.fieldError(name);
    const labelId = `${name}-label`;
    const errorId = `${name}-error`;
    return html`<label class="notes-field">
      <span id=${labelId}>${label}</span>
      <textarea
        name=${name}
        .value=${this.fieldValue(name, value)}
        aria-labelledby=${labelId}
        aria-invalid=${error ? "true" : "false"}
        aria-describedby=${error ? errorId : nothing}
      ></textarea>
      ${this.renderFieldError(name)}
    </label>`;
  }

  private renderValidationFooter() {
    const valid = this.canContinue();
    let statusClass = "validation-status";
    if (valid) statusClass += " valid";
    else if (this.previewDirty) statusClass += " pending";
    return html`<div class="validation-footer">
      <p class=${statusClass} role="status">${this.validationStatus(valid)}</p>
      ${valid ? this.renderContinueButton() : this.renderValidateButton()}
    </div>`;
  }

  private validationStatus(valid: boolean) {
    if (valid) return html`<span aria-hidden="true">✓</span> Profile validated`;
    if (this.contributionBusy)
      return "Checking your metadata and generated profile…";
    if (this.contributionError || Object.keys(this.fieldErrors).length) {
      return "Review the validation errors above, then validate again.";
    }
    return this.previewDirty
      ? "Your changes have not been validated yet."
      : "Validate your metadata before continuing.";
  }

  private renderContinueButton() {
    return html`<button
      class="primary"
      type="button"
      @click=${() => {
        if (this.canContinue()) this.emit("profile-submit");
      }}
    >
      Continue to submit profile
    </button>`;
  }

  private renderValidateButton() {
    let label = "Validate profile";
    if (this.contributionBusy) label = "Validating profile…";
    else if (this.previewDirty) label = "Validate changes";
    return html`<button
      class="primary"
      type="submit"
      ?disabled=${this.contributionBusy}
    >
      ${label}
    </button>`;
  }

  private renderContributionAuthShortcut() {
    if (this.contributionAuth?.connected) {
      const login = this.contributionAuth.identity?.login ?? "GitHub";
      return html`<p class="notice" role="status">
        Connected to GitHub as ${login}.
      </p>`;
    }
    return html` <div class="auth-shortcut">
      <span
        >Connect GitHub in settings before confirming an automatic
        contribution.</span
      >
      <button type="button" @click=${this.openGithubSettings}>
        Open GitHub settings
      </button>
    </div>`;
  }

  private renderMeasurementContext(draft: ContributionDraft) {
    const entries = Object.entries(draft.home_assistant).filter(
      ([, value]) => value !== null && value !== "",
    );
    if (!entries.length) return nothing;
    return html` <details class="profile-details">
      <summary>Measurement context</summary>
      <div class="profile-details-body">
        <dl class="info-list" aria-label="Home Assistant measurement context">
          ${entries.map(
            ([label, value]) =>
              html`<div>
                <dt><span>Home Assistant ${words(label)}</span></dt>
                <dd>${value}</dd>
              </div>`,
          )}
        </dl>
      </div>
    </details>`;
  }

  private renderMainsVoltage(draft: ContributionDraft) {
    if (draft.voltage_range) {
      return html` <label>
        <span>Nominal mains voltage</span>
        <input
          type="text"
          .value=${`${draft.mains_voltage ?? "—"} V`}
          readonly
        />
        <small class="field-hint"
          >Calculated from the measured
          ${draft.voltage_range.min}–${draft.voltage_range.max} V range.</small
        >
      </label>`;
    }
    return html` <measure-combobox
      name="mains_voltage"
      label="Nominal mains voltage"
      .value=${this.fieldValue("mains_voltage", draft.mains_voltage)}
      .options=${[120, 230].map((voltage) => ({
        value: String(voltage),
        label: `${voltage} V`,
      }))}
      .error=${this.fieldError("mains_voltage")}
      ?disabled=${this.contributionBusy}
      placeholder="Select voltage"
      hint="The power meter did not report a voltage range, so select the nominal mains voltage used during measurement."
      required
    ></measure-combobox>`;
  }

  private renderDeviceSpecifications(draft: ContributionDraft) {
    const deviceType = this.deviceType(draft);
    let fields: DeviceSpecificationField[] = [];
    if (deviceType) fields = this.deviceSpecificationFields[deviceType] ?? [];
    const typeLabel = deviceType ? optionLabel(deviceType) : "this device type";
    return html` <fieldset
      class="metadata-group"
      ?disabled=${this.contributionBusy}
    >
      <legend>Device specifications</legend>
      <div class="metadata-group-body">
        <p class="metadata-group-description">
          Optional manufacturer specifications for ${typeLabel.toLowerCase()}
          profiles.
        </p>
        ${fields.length
          ? html`<div class="contribution-grid">
              ${fields.map((field) =>
                this.renderDeviceSpecification(
                  field,
                  draft.device_specs?.[field.name],
                ),
              )}
            </div>`
          : html`<p class="muted">
              Specification fields are currently unavailable. Existing values
              will be kept.
            </p>`}
        ${this.renderFieldError("device_specs")}
      </div>
    </fieldset>`;
  }

  private renderDeviceSpecification(
    field: DeviceSpecificationField,
    value: unknown,
  ) {
    const name = `device_specs.${field.name}`;
    const error = this.fieldError(name);
    if (field.collection !== "scalar") {
      return this.renderSpecificationCollection(field, name, value, error);
    }
    if (field.value_type === "boolean" || field.options.length) {
      return this.renderSpecificationChoice(field, name, value, error);
    }
    return this.renderSpecificationInput(field, name, value, error);
  }

  private renderSpecificationCollection(
    field: DeviceSpecificationField,
    name: string,
    value: unknown,
    error: string,
  ) {
    const selected = specificationValues(value);
    return html`<measure-combobox
      name=${name}
      label=${field.label}
      .error=${error}
      ?disabled=${this.contributionBusy}
      .value=${guard(
        [value, this.contributionFormValues[name]],
        () => this.contributionFormValues[name] ?? selected,
      )}
      .options=${field.options.map((option) => ({
        value: option,
        label: optionLabel(option),
      }))}
      placeholder="Select an option…"
      hint=${field.description}
      multiple
    ></measure-combobox>`;
  }

  private renderSpecificationChoice(
    field: DeviceSpecificationField,
    name: string,
    value: unknown,
    error: string,
  ) {
    const values =
      field.value_type === "boolean" ? ["true", "false"] : field.options;
    const options = values.map((option) => ({
      value: option,
      label: specificationOptionLabel(field, option),
    }));
    return html`<measure-combobox
      name=${name}
      label=${field.label}
      .value=${this.fieldValue(name, value)}
      .options=${[{ value: "", label: "Not specified" }, ...options]}
      .error=${error}
      ?disabled=${this.contributionBusy}
      placeholder="Not specified"
      hint=${field.description}
    ></measure-combobox>`;
  }

  private renderSpecificationInput(
    field: DeviceSpecificationField,
    name: string,
    value: unknown,
    error: string,
  ) {
    const labelId = `${name}-label`;
    const hintId = `${name}-hint`;
    const errorId = `${name}-error`;
    const describedBy = [field.description ? hintId : "", error ? errorId : ""]
      .filter(Boolean)
      .join(" ");
    const numeric =
      field.value_type === "number" || field.value_type === "integer";
    const label = specificationInputLabel(field);
    let step: string | typeof nothing = nothing;
    if (field.value_type === "integer") step = "1";
    else if (field.value_type === "number") step = "any";
    return html` <label>
      <span id=${labelId}>${label}</span>
      <input
        name=${name}
        aria-labelledby=${labelId}
        aria-invalid=${error ? "true" : "false"}
        aria-describedby=${describedBy || nothing}
        type=${numeric ? "number" : "text"}
        step=${step}
        .value=${this.fieldValue(name, value)}
      />
      ${field.description
        ? html`<small id=${hintId} class="field-hint"
            >${field.description}</small
          >`
        : nothing}
      ${this.renderFieldError(name)}
    </label>`;
  }

  private renderPreparedPreview(preview: ContributionPreview) {
    const files = preview.files
      .map((file) => formatPreparedFile(file))
      .join("\n");
    const model =
      preview.model_json ??
      preview.files.find((file) => file.path.endsWith("model.json"))
        ?.rendered_json ??
      {};
    return html`
      ${preview.warnings.map(
        (warning) =>
          html`<p class="notice warning preparation-warning">${warning}</p>`,
      )}
      <details class="profile-details prepared-preview">
        <summary>Prepared files (${preview.files.length})</summary>
        <div class="profile-details-body">
          <div class="preview-block">
            <span>Files</span>
            <pre>${files}</pre>
          </div>
          <div class="preview-block">
            <span>Generated model.json</span>
            <pre>${JSON.stringify(model, null, 2)}</pre>
          </div>
        </div>
      </details>
    `;
  }

  private renderGithubPreview(preview: ContributionPreview) {
    const baseRevision = preview.base_sha ? ` @ ${preview.base_sha}` : "";
    return html`
      <div class="preview-block">
        <span>Repository</span>
        <pre>
Upstream: ${preview.repository}
Fork: ${preview.fork_repository ?? "Created when submitted"}
Base: ${preview.base_branch}${baseRevision}
Branch: ${preview.branch_name}</pre
        >
      </div>
      <div class="preview-block">
        <span>Commit and pull request</span>
        <pre>
${preview.commit_message}

${preview.pr_title}

${preview.pr_body}</pre
        >
      </div>
      <label class="confirm-row">
        <input
          name="confirm_contribution"
          type="checkbox"
          @change=${() => this.requestUpdate()}
        />
        <span>I reviewed the exact files, commit, and pull request text.</span>
      </label>
      <div class="actions">
        <button
          class="primary"
          type="button"
          @click=${this.submitContribution}
          ?disabled=${!this.canSubmitContribution()}
        >
          ${this.contributionBusy
            ? "Opening pull request…"
            : "Confirm and open PR"}
        </button>
      </div>
    `;
  }

  private renderContributionResult() {
    if (!this.contributionResult) return nothing;
    if (this.contributionResult.pull_request_url) {
      return html`<a
        class="success-link"
        href=${this.contributionResult.pull_request_url}
        target="_blank"
        rel="noopener noreferrer"
        >View pull request</a
      >`;
    }
    return html`<p class="notice" role="status">
      ${this.contributionResult.message ?? "Contribution is being processed."}
    </p>`;
  }

  private summaryEntries(): [string, string][] {
    return this.snapshot.summary ? Object.entries(this.snapshot.summary) : [];
  }

  private renderSummary() {
    const entries = this.summaryEntries().filter(([label]) => !ANALYSIS_SUMMARY_LABELS.has(label));
    if (!entries.length) return nothing;
    return html`<div class="readout" aria-label="Measurement result">
      ${entries.map(
        ([label, value]) =>
          html`<div class="metric">
            <span>${label}</span><strong>${value}</strong>
          </div>`,
      )}
    </div>`;
  }

  private renderAnalysis() {
    const entries = this.summaryEntries().filter(([label]) => ANALYSIS_SUMMARY_LABELS.has(label));
    if (!entries.length && !this.canAnalyse) return nothing;
    const result = entries.find(([label]) => label === "Recording analysis")?.[1]
      ?? entries.find(([label]) => label === "Profile analysis")?.[1];
    const reason = entries.find(([label]) => label === "Recording analysis reason")?.[1]
      ?? entries.find(([label]) => label === "Profile analysis reason")?.[1];
    const feature = entries.find(([label]) => label === "Analysed feature")?.[1];
    const details = entries.filter(([label]) => !label.endsWith("analysis") && !label.endsWith("analysis reason"));
    return html`
      <section class="analysis-panel" aria-labelledby="recording-analysis-title">
        <h3 id="recording-analysis-title">Recording analysis</h3>
        <p class="analysis-explanation">
          ${feature
            ? html`PowerCalc analysed how the measured power changed for each value of <code>${this.analysisFeature(feature)}</code>. This creates a profile that can estimate power from that entity data.`
            : "PowerCalc compared the measured power with changes in the recorded entity states to create a suitable power profile."}
        </p>
        ${result ? html`<p class="analysis-outcome"><span>Result</span><strong>${this.analysisResult(result)}</strong></p>` : nothing}
        ${reason ? html`<p class="analysis-reason"><strong>Why:</strong> ${reason}</p>` : nothing}
        ${details.length ? html`<dl class="analysis-details" aria-label="Recording analysis details">
          ${details.map(([label, value]) => {
            const displayLabel = this.analysisDetailLabel(label);
            const help = this.analysisDetailHelp(label);
            return html`
              <div>
                <dt>
                  <span>${displayLabel}</span>
                  ${help ? html`<span class="analysis-help" tabindex="0" data-help=${help} title=${help} aria-label=${`${displayLabel}: ${help}`}>?</span>` : nothing}
                </dt>
                <dd>${label === "Analysed feature" ? this.analysisFeature(value) : value}</dd>
              </div>
            `;
          })}
        </dl>` : nothing}
        ${this.canAnalyse ? html`
          <div class="analysis-retry">
            <p class=${this.analysisComplete ? "complete" : ""} role=${this.analysisComplete ? "status" : nothing}>
              ${this.busy
                ? "Analysing the saved recording and refreshing the result…"
                : this.analysisComplete
                  ? "✓ Recording analysed again. The result and generated files are now up to date."
                  : html`Run the saved <code>record.jsonl</code> through the current analyser again. No new measurement is needed.`}
            </p>
            <button type="button" @click=${() => this.emit("analyse")} ?disabled=${this.busy}>
              ${this.busy ? "Analysing…" : "Analyse recording again"}
            </button>
          </div>
        ` : nothing}
      </section>
    `;
  }

  private analysisResult(result: string): string {
    if (result === "Fixed power profile created") return "A fixed power profile was created.";
    return result === "Fixed states_power model created" || result === "Fixed states_power profile created"
      ? "A state-based power profile was created."
      : result;
  }

  private analysisDetailLabel(label: string): string {
    if (label === "Analysed feature") return "Model input";
    if (label === "Validation MAE") return "Typical difference";
    if (label === "Validation coverage") return "Data coverage";
    return label;
  }

  private analysisDetailHelp(label: string): string | undefined {
    if (label === "Analysed feature") {
      return "The Home Assistant entity data that best explained the measured power changes. The generated profile will use this as its input.";
    }
    if (label === "Validation MAE") {
      return "How closely the profile matched measurement samples it had not used to learn. This is the typical difference in watts; lower is better.";
    }
    if (label === "Validation coverage") {
      return "The share of those measurement samples for which the profile could estimate power. 100% means every sample was covered.";
    }
    return undefined;
  }

  private analysisFeature(feature: string): string {
    return feature.endsWith(".state") ? feature.slice(0, -".state".length) : feature;
  }

  private renderWarnings() {
    return (this.snapshot.warnings ?? []).map((warning) => html`<p class="notice" role="status">${warning}</p>`);
  }

  private isInspectableJson(file: SessionFile): boolean {
    const basename = file.name.split("/").at(-1) ?? file.name;
    return file.media_type === "application/json" && INSPECTABLE_JSON_FILES.has(basename);
  }

  private async openJsonInspector(name: string, trigger: HTMLElement): Promise<void> {
    this.inspectorTrigger = trigger;
    this.jsonInspector = { name };
    try {
      const content = await this.inspectJsonFile(name);
      if (this.jsonInspector?.name === name) this.jsonInspector = { name, content };
    } catch (error) {
      if (this.jsonInspector?.name === name) {
        this.jsonInspector = { name, error: error instanceof Error ? error.message : "Could not load this file." };
      }
    }
  }

  private closeJsonInspector(): void {
    this.shadowRoot?.querySelector<HTMLDialogElement>(".json-dialog")?.close();
    this.jsonInspector = undefined;
    this.inspectorTrigger?.focus();
  }

  private renderJsonInspector() {
    const inspector = this.jsonInspector;
    if (!inspector) return nothing;
    return html`
        <dialog class="json-dialog" role="dialog" aria-labelledby="json-dialog-title"
          @cancel=${(event: Event) => { event.preventDefault(); this.closeJsonInspector(); }}
          @click=${(event: MouseEvent) => {
            const dialog = event.currentTarget as HTMLDialogElement;
            const bounds = dialog.getBoundingClientRect();
            if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right
              || event.clientY < bounds.top || event.clientY > bounds.bottom)) this.closeJsonInspector();
          }}>
          <div class="json-dialog-header">
            <h3 id="json-dialog-title">${inspector.name}</h3>
            <button type="button" autofocus @click=${() => this.closeJsonInspector()}>Close</button>
          </div>
          ${this.renderJsonInspectorContent(inspector)}
        </dialog>
    `;
  }

  private renderJsonInspectorContent(inspector: { name: string; content?: unknown; error?: string }) {
    if (inspector.error) return html`<p class="notice error" role="alert">${inspector.error}</p>`;
    if (inspector.content === undefined) return html`<p class="muted" role="status">Loading JSON…</p>`;
    return html`<pre>${JSON.stringify(inspector.content, null, 2)}</pre>`;
  }

  private renderResume(state: SessionState) {
    // canResume already mirrors the backend's own RESUMABLE_SESSION_STATES (resumable,
    // cancelled, and failed -- see measure/ha_app/session.py), so this state check must
    // match it exactly. "failed" was missing here, which hid the button for a session
    // that failed cleanly mid-run (e.g. a transient meter read error) even though the
    // API's own /resume endpoint would accept it -- the sessions list's own Resume
    // button uses canResume alone and had no such gap.
    if (
      !this.canResume ||
      (state !== "resumable" && state !== "cancelled" && state !== "failed")
    )
      return nothing;
    return html`<button
      class="primary"
      type="button"
      @click=${() => this.emit("resume")}
      ?disabled=${this.busy}
    >${this.busy ? "Resuming…" : "Resume measurement"}</button>`;
  }

  /** How this outcome is announced. A completed run reads differently with and without a readout. */
  private outcome(state: SessionState): ResultOutcome {
    if (state !== "completed") return OUTCOMES[state] ?? STOPPED;
    return this.summaryEntries().length ? COMPLETED_WITH_READOUT : COMPLETED;
  }

  private input(
    name: keyof ContributionPreviewRequest,
    label: string,
    value: string,
    options: {
      required?: boolean;
      placeholder?: string;
      hint?: string;
      error?: string;
    } = {},
  ) {
    const { required = true, placeholder = "", hint = "" } = options;
    const error = options.error ?? this.fieldError(name);
    const labelId = `${name}-label`;
    const hintId = `${name}-hint`;
    const errorId = `${name}-error`;
    const requiredMarker = required
      ? html` <span class="required-marker" aria-hidden="true">*</span>`
      : nothing;
    const hintMarkup = hint
      ? html`<small id=${hintId} class="field-hint">${hint}</small>`
      : nothing;
    const errorMarkup = error
      ? html`<small id=${errorId} class="field-hint error">${error}</small>`
      : nothing;
    const describedBy = [hint ? hintId : "", error ? errorId : ""]
      .filter(Boolean)
      .join(" ");
    return html`
      <label>
        <span id=${labelId}>${label}${requiredMarker}</span>
        <input
          name=${name}
          type=${name === "contributor_email" ? "email" : "text"}
          .value=${this.fieldValue(name, value)}
          ?required=${required}
          placeholder=${placeholder}
          autocomplete="off"
          aria-invalid=${error ? "true" : "false"}
          aria-labelledby=${labelId}
          aria-describedby=${describedBy || nothing}
        />
        ${hintMarkup} ${errorMarkup}
      </label>
    `;
  }

  private collectContribution(): ContributionPreviewRequest | null {
    const form =
      this.shadowRoot?.querySelector<HTMLFormElement>(".contribution-form");
    if (!form) return this.submitMode ? this.preparedContribution() : null;
    const data = submittedForm(form);
    const draft = this.editableDraft();
    let fields: DeviceSpecificationField[] = [];
    if (draft)
      fields = this.deviceSpecificationFields[this.deviceType(draft)] ?? [];
    const deviceSpecs = fields.length
      ? collectDeviceSpecifications(form, data, fields)
      : (draft?.device_specs ?? null);
    const mainsVoltageControl = form.querySelector(
      'measure-combobox[name="mains_voltage"]',
    ) as (HTMLElement & { value?: string }) | null;
    const mainsVoltageValue = contributionMainsVoltage(
      data,
      mainsVoltageControl,
      draft,
    );
    return {
      manufacturer_name: formText(data, "manufacturer_name"),
      model_id: formText(data, "model_id"),
      product_name: formText(data, "product_name"),
      contributor: formText(data, "contributor"),
      contributor_github: formText(data, "contributor_github"),
      contributor_email: formText(data, "contributor_email"),
      aliases: formList(data, "aliases"),
      gtins: formList(data, "gtins"),
      product_url: formText(data, "product_url"),
      mains_voltage: mainsVoltageValue ? Number(mainsVoltageValue) : null,
      device_specs: deviceSpecs,
      measure_device: formText(data, "measure_device"),
      measure_device_firmware: formText(data, "measure_device_firmware"),
      measure_description: formText(data, "measure_description"),
      notes: formText(data, "notes"),
    };
  }

  private preparedContribution(): ContributionPreviewRequest | null {
    const draft = this.contributionPreview;
    if (!draft) return null;
    return {
      manufacturer_name: draft.manufacturer_name,
      model_id: draft.model_id,
      product_name: draft.product_name,
      contributor: draft.contributor,
      contributor_github: draft.contributor_github ?? "",
      contributor_email: draft.contributor_email ?? "",
      aliases: draft.aliases ?? [],
      gtins: draft.gtins ?? [],
      product_url: draft.product_url ?? "",
      mains_voltage: draft.mains_voltage ?? null,
      device_specs: draft.device_specs ?? null,
      measure_device: draft.measure_device ?? "",
      measure_device_firmware: draft.measure_device_firmware ?? "",
      measure_description: draft.measure_description ?? "",
      notes: draft.notes,
    };
  }

  private previewContribution(event: SubmitEvent): void {
    event.preventDefault();
    if (this.contributionBusy) return;
    const detail = this.collectContribution();
    if (!detail) return;
    this.contributionEdit = detail;
    this.fieldErrors = validateMetadata(detail);
    const form = this.shadowRoot?.querySelector(".contribution-form");
    for (const input of form?.querySelectorAll<HTMLInputElement>(
      'input[type="number"]',
    ) ?? []) {
      if (!input.validity.valid)
        this.fieldErrors[input.name] = input.validity.badInput
          ? "Enter a number."
          : "Enter a whole number.";
    }
    if (Object.keys(this.fieldErrors).length) {
      this.previewDirty = true;
      void this.updateComplete.then(() => this.focusValidation());
      return;
    }
    emit<ContributionPreviewRequest>(this, "contribution-preview", detail);
  }

  private submitContribution(): void {
    const detail = this.collectContribution();
    if (!detail || !this.canSubmitContribution()) return;
    emit<ContributionSubmitRequest>(this, "contribution-submit", {
      ...detail,
      confirmed: true,
    });
  }

  private canSubmitContribution(): boolean {
    const confirmed =
      this.shadowRoot?.querySelector<HTMLInputElement>(
        'input[name="confirm_contribution"]',
      )?.checked ?? false;
    return Boolean(
      confirmed &&
      this.contributionPreview &&
      !this.previewDirty &&
      this.contributionAuth?.connected &&
      !this.contributionBusy,
    );
  }

  private openGithubSettings(): void {
    emit<{ section: SettingsSection }>(this, "open-settings", {
      section: "github",
    });
  }

  private fieldError(name: string): string {
    const clientError = this.fieldErrors[name];
    if (clientError) return clientError;
    if (
      this.contributionErrorField === name &&
      this.dismissedServerField !== name
    )
      return this.contributionError;
    return "";
  }

  private renderFieldError(name: string) {
    const error = this.fieldError(name);
    if (!error) return nothing;
    const id = `${name}-error`;
    return html`<small id=${id} class="field-hint error">${error}</small>`;
  }

  private validationErrors(): Record<string, string> {
    const errors = { ...this.fieldErrors };
    if (
      this.contributionError &&
      this.dismissedServerField !== this.contributionErrorField
    ) {
      errors[this.contributionErrorField ?? ""] = this.contributionError;
    } else if (this.contributionError && !this.contributionErrorField)
      errors[""] = this.contributionError;
    return errors;
  }

  private renderValidationSummary() {
    const errors = Object.entries(this.validationErrors());
    if (!errors.length) return nothing;
    return html`<div
      class="notice error validation-summary"
      role="alert"
      tabindex="-1"
    >
      <strong>Check these profile details:</strong>
      <ul>
        ${errors.map(([name, message]) =>
          this.renderValidationError(name, message),
        )}
      </ul>
    </div>`;
  }

  private renderValidationError(name: string, message: string) {
    const knownSpec = Object.values(this.deviceSpecificationFields)
      .flat()
      .some((field) => name === `device_specs.${field.name}`);
    const focusable =
      (name in metadataLabels && name !== "device_specs") || knownSpec;
    const content = focusable
      ? html`<button type="button" @click=${() => this.focusField(name)}>
          ${this.fieldLabel(name)}: ${message}
        </button>`
      : message;
    return html`<li>${content}</li>`;
  }

  private fieldLabel(name: string): string {
    const spec = Object.values(this.deviceSpecificationFields)
      .flat()
      .find((field) => `device_specs.${field.name}` === name);
    return metadataLabels[name] ?? spec?.label ?? words(name);
  }

  private fieldControl(name: string): HTMLElement | undefined {
    return Array.from(
      this.shadowRoot?.querySelectorAll<HTMLElement>("[name]") ?? [],
    ).find(
      (control) =>
        control.getAttribute("name") === name &&
        control.getAttribute("type") !== "hidden",
    );
  }

  private focusField(name: string): void {
    const control = this.fieldControl(name);
    const input =
      control?.shadowRoot?.querySelector<HTMLElement>("input, select") ??
      control;
    input?.focus();
    input?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }

  private focusValidation(): void {
    const first = Object.keys(this.validationErrors()).find((name) =>
      this.fieldControl(name),
    );
    if (first) this.focusField(first);
    else {
      const summary = this.shadowRoot?.querySelector<HTMLElement>(
        ".validation-summary",
      );
      summary?.focus();
      summary?.scrollIntoView?.({ block: "center" });
    }
  }

  private metadataChanged(event: Event): void {
    const control = event.target as HTMLElement & {
      name?: string;
      value?: string | string[];
    };
    const name = control.name;
    if (!name || name === "confirm_contribution") return;
    if (control.value !== undefined) {
      this.contributionFormValues = {
        ...this.contributionFormValues,
        [name]: control.value,
      };
      emit(this, "contribution-edit", this.contributionFormValues);
    }
    this.previewDirty = true;
    const errors = { ...this.fieldErrors };
    delete errors[name];
    this.fieldErrors = errors;
    if (this.contributionErrorField === name) this.dismissedServerField = name;
  }

  private fieldValue(name: string, fallback: unknown): string {
    const value = this.contributionFormValues[name] ?? fallback;
    return formValue(value);
  }

  private fieldList(name: string, fallback: string[]): string[] {
    const value = this.contributionFormValues[name] ?? fallback;
    if (Array.isArray(value)) return value.map(String);
    if (typeof value === "string" && value.trim()) {
      return value.split(",").map((item) => item.trim()).filter(Boolean);
    }
    return fallback;
  }

  private validateField(event: FocusEvent): void {
    const control = event.target as HTMLElement & { name?: string };
    const name = control.name;
    if (
      !name ||
      this.contributionBusy ||
      event.relatedTarget === control ||
      control.contains(event.relatedTarget as Node | null)
    )
      return;
    const values = this.collectContribution();
    if (!values) return;
    const validation = validateMetadata(values);
    const errors = { ...this.fieldErrors };
    for (const field of name === "manufacturer_name"
      ? [name, "product_name"]
      : [name]) {
      if (validation[field]) errors[field] = validation[field];
      else delete errors[field];
    }
    this.fieldErrors = errors;
  }

  private canContinue(): boolean {
    return Boolean(
      this.contributionPreview &&
      !this.previewDirty &&
      !this.contributionBusy &&
      !this.contributionError &&
      !Object.keys(this.fieldErrors).length,
    );
  }

  private editableDraft(): ContributionDraft | undefined {
    const source = this.contributionPreview ?? this.contributionDraft;
    return source && this.contributionEdit
      ? { ...source, ...this.contributionEdit }
      : source;
  }

  private deviceType(draft: ContributionDraft): string {
    if (draft.device_type) return draft.device_type;
    if (
      typeof draft.model_json !== "object" ||
      draft.model_json === null ||
      Array.isArray(draft.model_json)
    )
      return "";
    const value = (draft.model_json as Record<string, unknown>).device_type;
    return typeof value === "string" ? value : "";
  }

  private emit(
    name:
      | "sessions"
      | "new"
      | "resume"
      | "refine"
      | "analyse"
      | "prepare"
      | "profile-submit"
      | "back",
  ): void {
    emit(this, name);
  }
}

function collectDeviceSpecifications(
  form: HTMLFormElement,
  data: FormData,
  fields: DeviceSpecificationField[],
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const field of fields) {
    const name = `device_specs.${field.name}`;
    if (field.collection !== "scalar") {
      collectSpecificationCollection(result, form, data, field, name);
      continue;
    }
    collectScalarSpecification(result, form, data, field, name);
  }
  return result;
}

function collectSpecificationCollection(
  result: Record<string, unknown>,
  form: HTMLFormElement,
  data: FormData,
  field: DeviceSpecificationField,
  name: string,
): void {
  const control = Array.from(form.querySelectorAll("measure-combobox")).find(
    (candidate) => candidate.multiple && candidate.name === name,
  );
  const controlValues = Array.isArray(control?.value) ? control.value : [];
  const submitted = data.getAll(name).map(String);
  const values = (submitted.length ? submitted : controlValues).filter(Boolean);
  if (!values.length) return;
  result[field.name] =
    field.collection === "scalar_or_array" && values.length === 1
      ? values[0]
      : values;
}

function collectScalarSpecification(
  result: Record<string, unknown>,
  form: HTMLFormElement,
  data: FormData,
  field: DeviceSpecificationField,
  name: string,
): void {
  const control = Array.from(form.querySelectorAll("measure-combobox")).find(
    (candidate) => !candidate.multiple && candidate.name === name,
  );
  const controlValue = typeof control?.value === "string" ? control.value : "";
  const value = formText(data, name) || controlValue;
  if (!value) return;
  if (field.value_type === "number" || field.value_type === "integer")
    result[field.name] = Number(value);
  else if (field.value_type === "boolean")
    result[field.name] = value === "true";
  else result[field.name] = value;
}

function specificationValues(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (value === undefined || value === null) return [];
  return [formValue(value)];
}

function specificationOptionLabel(
  field: DeviceSpecificationField,
  option: string,
): string {
  if (field.value_type !== "boolean") return optionLabel(option);
  return option === "true" ? "Yes" : "No";
}

function specificationInputLabel(field: DeviceSpecificationField): string {
  if (field.name === "rated_power") return "Rated power (W)";
  if (field.name === "lumens") return "Light output (lm)";
  return field.label;
}

function contributionMainsVoltage(
  data: FormData,
  control: (HTMLElement & { value?: string }) | null,
  draft: ContributionDraft | undefined,
): string {
  const submitted = formText(data, "mains_voltage");
  if (submitted) return submitted;
  if (typeof control?.value === "string" && control.value) return control.value;
  return formValue(draft?.mains_voltage);
}

function formValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return "";
}

function formatPreparedFile(file: ContributionDraftFile): string {
  return file.size === undefined
    ? file.path
    : `${file.path} (${fileSize(file.size)})`;
}

const MERGE_REASON_LABELS: Record<string, string> = {
  settle: "settled over timed-out",
  samples: "more readings",
  witness: "witness agreement",
  newer: "newer",
};

function formatMergeMode(mode: string, stats: MergeModePreview): string {
  const parts: string[] = [];
  if (stats.added) parts.push(`+${stats.added} new`);
  if (stats.kept || stats.replaced)
    parts.push(`${stats.kept} kept / ${stats.replaced} replaced`);
  const reasons = Object.entries(stats.replaced_reasons ?? {})
    .filter(([, count]) => count > 0)
    .map(
      ([reason]) => MERGE_REASON_LABELS[reason] ?? reason.replaceAll("_", " "),
    );
  const label = mode === "color_temp" ? "CT" : mode.toUpperCase();
  return `${label}: ${parts.join(", ") || "no changes"}${reasons.length ? ` (${reasons.join(", ")})` : ""}`;
}

function optionLabel(value: string): string {
  const abbreviations: Record<string, string> = {
    rf433: "RF 433",
    usb: "USB",
    wifi: "Wi-Fi",
    zwave: "Z-Wave",
  };
  if (abbreviations[value]) return abbreviations[value];
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
