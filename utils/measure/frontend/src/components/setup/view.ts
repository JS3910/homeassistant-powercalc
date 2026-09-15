import { LitElement, css, html, nothing } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type {
  Capabilities,
  DummyLoadCalibration,
  DummyLoadSpec,
  EntityDescriptor,
  ErrorHelp,
  FormField,
  FormFieldOption,
  LightEstimate,
  LutMode,
  MeasureDefinition,
  MeasureParameter,
  MeasureParameterName,
  MeasureType,
  MeasurementRequest,
  PowerMeterSpec,
  ResolutionAxis,
} from "../../types";
import {
  hasVoltageReading,
  specHasOcr,
  supportsDummyLoad,
} from "../../power-meter/registry";
import type { MeterContext } from "../../power-meter/registry";
import {
  buildMeasurementRequest,
  deviceFields,
  colorModesCoverBrightness,
  enabledParameters,
  entityDomain,
  gatedParameters,
  entityDomains,
  fieldVisible,
  fieldOptions,
  narrowingField,
  requestFieldValue,
} from "../../measurement/definition";
import { emit } from "../../utils/events";
import { formText } from "../../utils/form";
import {
  brightnessPercent,
  duration,
  hueDegrees,
  kelvinToMired,
  miredToKelvin,
  sessionModeLabel,
  snapKelvinToMired,
  stepKelvinToMired,
} from "../../utils/format";
import { sharedStyles } from "../../styles";
import {
  entitySelect,
  fieldHint,
  numberField,
  optionSelect,
  sliderNumberField,
  textField,
} from "../shared/fields";
import {
  defaultDummyLoadMode,
  dummyLoadSpec,
  dummyLoadStyles,
  renderDummyLoad,
} from "./dummy-load-field";
import { entityListStyles, renderEntityList } from "./entity-list-field";
import type { Combobox } from "../shared/combobox";
import {
  renderPowerMeterRequired,
  renderPowerMeterSummary,
  renderTypeChip,
  renderTypePicker,
  setupChromeStyles,
} from "./setup-chrome";
import { errorHelpLink } from "../shared/error-help-link";
import "./refine-banner";
import "../shared/ocr-preview";

const ESTIMATE_DEBOUNCE_MS = 300;
const RANGE_BOUNDS_GROUP = "Range bounds";
const RESOLUTION_GROUP = "Profile resolution";
const CARTESIAN_RESOLUTION = new Set(["bri_bri_steps"]);
/** Still used in Smart mode: the 100% color outline and forced-ramp brightness steps. */
const SMART_SWEEPS = new Set([
  "ct_mired_divisions",
  "hs_hue_divisions",
  "hs_sat_divisions",
  "ct_bri_steps",
  "hs_bri_steps",
]);
const SMART_SWEEP_LABELS: Record<string, string> = {
  ct_mired_divisions: "100% color temperature sweep",
  hs_hue_divisions: "100% hue sweep",
  hs_sat_divisions: "100% saturation samples",
  ct_bri_steps: "Forced CT brightness step",
  hs_bri_steps: "Forced HS brightness step",
};

const LIGHT_DISCOVERY_HINT =
  "Newly discovered lights may not have a usable state until Home Assistant receives their first update. If a light is missing, change its state once in Home Assistant, then reload this page.";

const MULTIPLE_LIGHTS_GUIDE_URL =
  "https://docs.powercalc.nl/contributing/measure/lights/#multiple-identical-lights";
const HOME_ASSISTANT_GROUP_GUIDE_URL =
  "https://www.home-assistant.io/integrations/group/";

/** Human unit beside the native HA value. Sweep-count sliders stay a count. */
function sliderSecondaryUnit(
  parameter: MeasureParameter,
  numeric: number,
  snappedKelvin: { mired: number } | null,
): string | undefined {
  if (snappedKelvin != null) return `${snappedKelvin.mired} mired`;
  if (!Number.isFinite(numeric)) return undefined;
  const bound = parameter.name.startsWith("min_") || parameter.name.startsWith("max_");
  if (!bound) return undefined;
  if (parameter.axis === "brightness" || parameter.axis === "sat") {
    return `${brightnessPercent(numeric)}%`;
  }
  if (parameter.axis === "hue") return `${hueDegrees(numeric)}°`;
  return undefined;
}

@customElement("measure-setup-view")
export class SetupView extends LitElement {
  @property({ attribute: false })
  capabilities?: Capabilities;

  @property({ attribute: false })
  definitions: MeasureDefinition[] = [];

  @property({ attribute: false })
  lights: EntityDescriptor[] = [];

  @property({ attribute: false })
  powers: EntityDescriptor[] = [];

  @property({ attribute: false })
  voltages: EntityDescriptor[] = [];

  @property({ attribute: false })
  deviceEntities: Record<string, EntityDescriptor[]> = {};

  @property({ attribute: false })
  deviceEntityErrors: Record<string, string> = {};

  @property({ attribute: false })
  initialRequest?: MeasurementRequest;

  @property({ attribute: false })
  dummyLoadCalibration: DummyLoadCalibration | null = null;

  @property({ attribute: false })
  initialType?: MeasureType;

  /** How the power meter this measurement uses is addressed. */

  @property({ attribute: false })
  meter: PowerMeterSpec = { type: "hass", entity_id: "" };

  @property({ type: String })
  defaultMeasureDevice = "";

  @property({ type: Boolean })
  powerMeterConfigured = true;

  @property({ type: Boolean })
  busy = false;

  @property({ type: String })
  busyDetail = "";

  @property({ type: String })
  errorMessage = "";

  @property({ attribute: false })
  errorHelp?: ErrorHelp;

  /** Throwaway OCR aiming preview started from Defaults / this draft -- shown as soon
   * as a camera is in the plan, not only after a session is running. */
  @property({ type: String })
  meterPreviewId: string | null = null;

  @property({ attribute: false })
  meterPreviewLabels: string[] = [];

  /** Cheap `/api/estimate` for the live resolution summary. Tests inject a stub. */
  @property({ attribute: false })
  loadEstimate?: (request: MeasurementRequest) => Promise<LightEstimate>;

  @state()
  selectedType?: MeasureType;

  /** Entities picked per field. A single-entity field simply holds a one-entry list. */

  @state()
  selectedEntities: Record<string, string[]> = {};

  @state()
  selectValues: Record<string, string> = {};

  @state()
  multiSelection: Record<string, string[]> = {};

  @state()
  parameterValues: Partial<Record<MeasureParameterName, string>> = {};

  @state()
  parameterFlags: Partial<Record<MeasureParameterName, boolean>> = {};

  @state()
  private estimate?: LightEstimate | null;

  @state()
  private estimateError = "";

  @state()
  private remeasureExisting = false;

  @state()
  dummyLoadEnabled = false;

  @state()
  dummyLoadMode: DummyLoadSpec["mode"] = "calibrate";

  @state()
  dummyController = false;

  @state()
  multipleLights = false;

  /** Deliberately not reactive: it exists so a typed count survives re-renders instead of being recomputed. */
  private derivedCountOverride?: string;
  private estimateTimer?: number;
  private estimateGeneration = 0;

  static readonly styles = [
    sharedStyles,
    dummyLoadStyles,
    entityListStyles,
    setupChromeStyles,
    css`
      :host {
        display: block;
        min-width: 0;
        max-width: 100%;
      }
      form {
        display: grid;
        gap: 1rem;
      }
      .profile-grid {
        align-items: start;
      }
      .device-section {
        display: grid;
        gap: 1rem;
        min-width: 0;
      }
      .light-grid > measure-combobox,
      .light-grid > .entity-list,
      .light-grid > .field-block {
        grid-column: 1 / -1;
      }
      .checks {
        display: flex;
        flex-wrap: wrap;
        gap: 0.6rem;
      }
      .check {
        min-height: 42px;
        padding: 0 0.75rem;
        border: 1px solid var(--line);
        border-radius: 999px;
      }
      /* A checkbox pill has no caption above it, so pin it to the input line of its row. */
      .profile-grid > .check {
        align-self: end;
      }
      details {
        border-top: 1px solid var(--line);
        padding-top: 1rem;
      }
      summary {
        width: fit-content;
        color: var(--signal-strong);
        cursor: pointer;
        font-weight: 700;
      }
      details .grid {
        margin-top: 0;
      }
      .ocr-previews {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
        gap: 1rem;
        margin: 0 0 1.25rem;
      }
      @media (max-width: 720px) {
        .ocr-previews {
          grid-template-columns: 1fr;
        }
      }
      .field-with-help {
        position: relative;
        min-width: 0;
      }
      details.context-help {
        border: 0;
        padding: 0;
      }
      .context-help > summary {
        position: absolute;
        top: 0;
        right: 0;
        display: grid;
        place-items: center;
        width: 24px;
        height: 24px;
        padding: 0;
        list-style: none;
      }
      .context-help > summary::-webkit-details-marker {
        display: none;
      }
      .context-help svg {
        width: 18px;
        height: 18px;
      }
      .help-content {
        position: absolute;
        top: 1.9rem;
        right: 0;
        z-index: 25;
        width: min(28rem, 100%);
        box-sizing: border-box;
        padding: 0.85rem 1rem;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--surface-raised);
        box-shadow: 0 10px 28px rgb(0 0 0 / 35%);
        color: var(--ink);
        font-size: 0.82rem;
        line-height: 1.5;
      }
      .help-content p {
        margin: 0;
      }
      .help-content p + p {
        margin-top: 0.65rem;
      }
      .developer-content {
        display: grid;
        gap: 0.75rem;
        margin-top: 0.75rem;
      }
      .developer-content .notice {
        margin: 0;
      }
      .test-mode-status {
        margin: 0;
        color: var(--signal-strong);
        font-size: 0.82rem;
      }
      .advanced-heading {
        grid-column: 1 / -1;
        margin: 0.25rem 0 -0.25rem;
        color: var(--signal-strong);
        font-size: 0.76rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      .context p {
        margin-bottom: 0;
      }

      .dummy-load {
        display: grid;
        gap: 0.9rem;
      }
      .dummy-load-toggle {
        width: fit-content;
      }
      /* A checkbox that reveals or hides a block of the form, rather than submitting a value. */
      .toggle-pill {
        width: fit-content;
      }
      .dummy-controller {
        display: grid;
        gap: 0.4rem;
      }
      .dummy-controller p {
        margin: 0;
      }
      .multiple-lights {
        padding-right: 2rem;
      }
      .multiple-lights .toggle-pill {
        min-height: 28px;
        padding: 0;
        border: 0;
        border-radius: 0;
      }
      .dummy-load-options {
        display: grid;
        gap: 0.8rem;
        padding: 0.9rem;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--field);
      }
      .dummy-load-options p {
        margin: 0;
      }
      .calibration-card {
        display: grid;
        gap: 0.2rem;
      }
      .calibration-card strong {
        color: var(--ink);
      }
      .calibration-meta {
        color: var(--muted);
        font-size: 0.78rem;
      }
      .choice-list {
        display: grid;
        gap: 0.5rem;
      }
      .choice {
        display: flex;
        grid-template-columns: none;
        align-items: flex-start;
        gap: 0.55rem;
        color: var(--ink);
      }
      .choice input {
        width: auto;
        min-height: auto;
        margin-top: 0.2rem;
        accent-color: var(--signal);
      }
      .field-block {
        display: grid;
        gap: 0.45rem;
      }
      .field-block .field-hint,
      .select-guidance p {
        margin: 0;
      }
      .select-guidance {
        display: grid;
        gap: 0.35rem;
      }
      .select-guidance ul {
        margin: 0.1rem 0 0;
        padding-left: 1.2rem;
        color: var(--muted);
        font-size: 0.82rem;
      }
      .tuning-groups {
        display: grid;
        gap: 1rem;
        margin-top: 1rem;
      }
      .slider-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 1rem;
      }
      .range-with-estimate {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 1rem;
        align-items: stretch;
      }
      .range-with-estimate .range-sliders {
        grid-column: 1 / 3;
        grid-row: 1;
        display: grid;
        gap: 1rem;
      }
      .range-with-estimate .range-sliders .slider-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .range-with-estimate .estimate-summary {
        grid-column: 3;
        grid-row: 1;
      }
      .resolution-field {
        display: grid;
        gap: 0.45rem;
        min-width: 0;
      }
      .resolution-field-row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        align-items: end;
      }
      .resolution-field-row .slider-number,
      .resolution-field-row > label:not(.check) {
        flex: 1 1 12rem;
        min-width: 0;
      }
      .estimate-summary {
        display: grid;
        align-content: start;
        gap: 0.25rem;
        padding: 0.75rem 0.85rem;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--field);
      }
      .estimate-summary p {
        margin: 0;
      }
      .estimate-summary .field-hint {
        margin-top: 0.2rem;
      }

      @media (max-width: 640px) {
        .context {
          display: block;
        }
        .slider-grid {
          grid-template-columns: 1fr;
        }
        .range-with-estimate,
        .range-with-estimate .range-sliders .slider-grid {
          grid-template-columns: 1fr;
        }
        .range-with-estimate .range-sliders,
        .range-with-estimate .estimate-summary {
          grid-column: auto;
          grid-row: auto;
        }
      }
    `,
  ];

  willUpdate(changed: Map<string, unknown>): void {
    // Restore the previously chosen type when returning from the review step.
    if (
      changed.has("initialType") &&
      this.initialType &&
      this.selectedType === undefined
    ) {
      this.selectedType = this.initialType;
    }
    if (changed.has("initialRequest")) {
      const controller =
        this.initialRequest && "controller" in this.initialRequest
          ? this.initialRequest.controller
          : undefined;
      this.remeasureExisting = Boolean(this.initialRequest?.remeasure_existing);
      this.dummyLoadEnabled = Boolean(this.initialRequest?.dummy_load);
      this.dummyLoadMode =
        this.initialRequest?.dummy_load?.mode ??
        defaultDummyLoadMode(this.dummyLoadCalibration);
      this.dummyController = controller?.type === "dummy";
      this.multipleLights = Boolean(
        this.initialRequest?.measure_type === "light" &&
        (this.initialRequest.controller.type === "hass_multi" ||
          this.initialRequest.multiple_light_count > 1),
      );
    } else if (changed.has("dummyLoadCalibration") && !this.dummyLoadEnabled) {
      this.dummyLoadMode = defaultDummyLoadMode(this.dummyLoadCalibration);
    }
  }

  protected updated(changed: Map<string, unknown>): void {
    if (
      changed.has("selectedType") ||
      changed.has("lights") ||
      changed.has("capabilities") ||
      changed.has("initialRequest") ||
      changed.has("dummyController") ||
      changed.has("parameterValues") ||
      changed.has("parameterFlags") ||
      changed.has("remeasureExisting") ||
      changed.has("multiSelection") ||
      changed.has("selectedEntities") ||
      changed.has("loadEstimate")
    ) {
      this.scheduleEstimate();
    }
  }

  disconnectedCallback(): void {
    window.clearTimeout(this.estimateTimer);
    super.disconnectedCallback();
  }

  render() {
    return html`
      <section class="panel" aria-labelledby="setup-title">
        <div class="context">
          <div>
            <p class="eyebrow">01 / Setup</p>
            <h2 id="setup-title">Configure the measurement</h2>
          </div>
        </div>
        ${this.initialRequest
          ? html`<p class="notice" role="status">
              This draft uses the selected session's measurement device and
              power-meter configuration.
              <button type="button" @click=${this.useCurrentSettings}>
                Use current app defaults
              </button>
            </p>`
          : nothing}
        ${this.renderRefineBanner()}
        ${this.powerMeterConfigured
          ? this.renderSetupContent()
          : renderPowerMeterRequired(this.openSettings)}
      </section>
    `;
  }

  private renderSetupContent() {
    return html`
      ${this.renderOcrPreview()}
      ${this.selectedType
        ? html`<div class="setup-summary">
            ${renderTypeChip(
              this.selectedType,
              this.definition(this.selectedType),
              this.changeType,
            )}
            ${renderPowerMeterSummary({
              meter: this.meter,
              measureDevice: this.defaultMeasureDevice,
              context: this.meterContext(),
              onOpenSettings: this.openSettings,
            })}
          </div>`
        : renderTypePicker(this.definitions, this.selectType)}
      ${this.selectedType
        ? this.renderMeasurementForm(this.selectedType)
        : nothing}
    `;
  }

  private renderOcrPreview() {
    const previewId = this.meterPreviewId;
    if (previewId && this.meterPreviewLabels.length) {
      return html`
        <div class="ocr-previews">
          ${this.meterPreviewLabels.map(
            (label) =>
              html`<measure-ocr-preview
                kind="preview"
                sessionId=${previewId}
                label=${label}
              ></measure-ocr-preview>`,
          )}
        </div>
      `;
    }
    if (!specHasOcr(this.meter)) return nothing;
    return html`
      <div class="ocr-previews">
        <div class="notice warning" role="status">
          <p>Camera preview is not running.</p>
          <button
            type="button"
            @click=${() => emit(this, "ocr-preview-reconnect")}
          >
            Start preview
          </button>
        </div>
      </div>
    `;
  }

  private useCurrentSettings(): void {
    emit(this, "use-current-settings");
  }

  private isExtendDraft(): boolean {
    return (
      this.initialRequest?.resume_policy === "extend" &&
      Boolean(this.initialRequest.seed_session_id)
    );
  }

  private renderRefineBanner() {
    if (!this.isExtendDraft()) return nothing;
    return html`<measure-refine-banner
      .request=${{
        ...this.initialRequest!,
        remeasure_existing: this.remeasureExisting,
      }}
      @remeasure-change=${this.onRemeasureChange}
    ></measure-refine-banner>`;
  }

  private renderRefineHiddenFields() {
    if (!this.isExtendDraft() || !this.initialRequest?.seed_session_id)
      return nothing;
    return html`
      <input type="hidden" name="resume_policy" value="extend" />
      <input
        type="hidden"
        name="seed_session_id"
        value=${this.initialRequest.seed_session_id}
      />
      ${this.remeasureExisting
        ? html`<input type="hidden" name="remeasure_existing" value="on" />`
        : nothing}
    `;
  }

  private onRemeasureChange = (event: CustomEvent<boolean>): void => {
    this.remeasureExisting = event.detail;
  };

  private withRefineFields(request: MeasurementRequest): MeasurementRequest {
    if (!this.isExtendDraft() || !this.initialRequest?.seed_session_id)
      return request;
    return {
      ...request,
      resume_policy: "extend",
      seed_session_id: this.initialRequest.seed_session_id,
      remeasure_existing: this.remeasureExisting,
    };
  }

  private renderMeasurementForm(type: MeasureType) {
    const definition = this.definition(type);
    if (!definition || !this.capabilities)
      return html`<p class="muted">Loading measurement capabilities…</p>`;
    const run =
      this.initialRequest?.measure_type === type
        ? this.initialRequest
        : undefined;
    const fields = deviceFields(definition);
    const multipleController = this.multiControllerField();
    // A multi-select needs the room of its own fieldset; the rest are grid cells.
    const blocks = fields.filter(
      (field) =>
        field.control === "multi_select" && this.isFieldVisible(field, run),
    );
    const startImmediately = this.startsWithoutReview(type);
    const activeLightCheck =
      type === "light" && !this.dummyController && !this.dummyLoadEnabled;
    return html`
      <form @submit=${this.submitMeasurement}>
        ${this.renderRefineHiddenFields()}
        <div class="device-section">
          ${this.dummyController
            ? html`<p class="test-mode-status" role="status">
                Virtual device · test output only
              </p>`
            : nothing}
          ${multipleController && !this.dummyController
            ? this.renderMultipleLightsToggle(multipleController)
            : nothing}
          <div class="field-with-help">
            <div
              class="grid profile-grid ${type === "light" ? "light-grid" : ""}"
            >
              ${fields
                .filter((field) => field.control !== "multi_select")
                .map((field) => this.genericField(field, run))}
              ${this.dummyController ||
              !definition.fields.some((field) => field.role === "controller")
                ? textField("session_name", "Session name (optional)", {
                    value: run?.session_name ?? "",
                    placeholder: "e.g. Desk lamp test",
                    hint: "A label for finding this measurement later; it is not the product name.",
                  })
                : nothing}
            </div>
            ${type === "light" && !this.dummyController
              ? this.contextHelp(
                  "Light not found?",
                  html`<p>${LIGHT_DISCOVERY_HINT}</p>`,
                  "discovery-help",
                )
              : nothing}
          </div>
          ${blocks.map((field) => this.multiSelectField(field, run))}
        </div>

        ${this.renderDummyLoadSection(run?.dummy_load)}
        ${this.renderTuning(definition, run)}
        ${this.renderDeveloperOptions(definition)}
        ${this.errorMessage
          ? html`<p class="notice error" role="alert">
              ${this.errorMessage}${errorHelpLink(this.errorHelp)}
            </p>`
          : nothing}
        ${activeLightCheck
          ? html`<p class="muted">The setup check briefly controls the selected light at low-load settings and leaves it off.</p>`
          : nothing}
        ${activeLightCheck && !startImmediately
          ? html`
              <p class="muted">
                The next step reviews anything that needs confirmation before
                the light is measured.
              </p>
            `
          : nothing}
        ${activeLightCheck && this.busy
          ? html`
              <div class="notice" role="status" aria-live="polite">
                <strong>Checking low-load light settings…</strong>
                <p>Testing representative low-load points for the selected brightness, white-channel, and color modes. Each point uses the configured settle, sample, and retry settings.</p>
                <progress aria-label="Low-load light check in progress"></progress>
              </div>
            `
          : this.busy
          ? html`
              <div class="notice" role="status" aria-live="polite">
                <strong
                  >${startImmediately
                    ? "Starting measurement…"
                    : this.busyDetail || "Checking setup…"}</strong
                >
                <p>
                  ${startImmediately
                    ? "Connecting to the light and power meter, then beginning the first settle waits."
                    : this.busyDetail
                      ? "Same settle and meter rules as a measurement point."
                      : "Validating the selected devices before the review step."}
                </p>
              </div>
            `
          : nothing}
        <div class="actions">
          <button class="primary" type="submit" ?disabled=${this.busy}>${this.busy
            ? startImmediately && !activeLightCheck
              ? "Starting measurement…"
              : activeLightCheck
                ? "Checking light and setup…"
                : "Checking setup…"
            : activeLightCheck
              ? "Check light and setup"
              : startImmediately
                ? "Start measurement"
                : "Check setup"}</button>
        </div>
      </form>
    `;
  }

  private renderDeveloperOptions(definition: MeasureDefinition) {
    const hasController = definition.fields.some(
      (field) => field.role === "controller",
    );
    if (
      !(this.capabilities?.developer_mode && hasController) &&
      !this.capabilities?.fast_test_mode
    )
      return nothing;
    return html`<details class="developer-options">
      <summary>Developer options</summary>
      <div class="developer-content">
        ${hasController ? this.renderDummyControllerToggle() : nothing}
        ${this.capabilities?.fast_test_mode
          ? html`<p class="notice">
              <strong>Fast test mode is enabled.</strong> Dummy light, fan,
              speaker and charging runs use minimal waits and measurement
              points. Their output is for app testing only.
            </p>`
          : nothing}
      </div>
    </details>`;
  }

  private renderDummyControllerToggle() {
    if (!this.capabilities?.developer_mode) return nothing;
    return html`
      <div class="dummy-controller">
        <label class="check toggle-pill">
          <input
            type="checkbox"
            name="use_dummy_controller"
            .checked=${this.dummyController}
            @change=${this.dummyControllerChanged}
          />
          Use virtual device (developer)
        </label>
        ${this.dummyController
          ? html`<p class="muted">
              No real device is controlled during this measurement. Use it only
              to test the app itself.
            </p>`
          : nothing}
      </div>
    `;
  }

  private renderMultipleLightsToggle(field: FormField) {
    return html`
      <div class="multiple-lights field-with-help">
        <label class="check toggle-pill">
          <input
            type="checkbox"
            name="measure_multiple_lights"
            .checked=${this.multipleLights}
            data-field=${field.name}
            @change=${this.multipleLightsChanged}
          />
          Measure multiple lights
        </label>
        ${this.contextHelp(
          "About measuring multiple lights",
          html`<p>
              Measuring multiple identical lights together increases the load,
              making very low power use easier to measure accurately.
            </p>
            <p>
              Select every identical light you want on the meter. There is no three-light limit.
              A native Zigbee or Hue group is one radio
              command if you already have one. A
              <a
                href=${HOME_ASSISTANT_GROUP_GUIDE_URL}
                target="_blank"
                rel="noopener noreferrer"
                >Home Assistant light group</a
              >
              works too, but Home Assistant fans that out member by member, so
              the last bulb can turn on after the others.
            </p>
            <p>
              <a
                class="help-link"
                href=${MULTIPLE_LIGHTS_GUIDE_URL}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Learn more about measuring multiple identical lights"
                >Multiple-light measurement guide ↗</a
              >
            </p>`,
        )}
      </div>
    `;
  }

  private contextHelp(label: string, content: unknown, className = "") {
    return html`<details
      class="context-help ${className}"
      @keydown=${this.helpKeydown}
      @focusout=${this.helpFocusOut}
    >
      <summary aria-label=${label} title=${label}>
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="9"></circle>
          <path
            d="M9.75 9a2.4 2.4 0 0 1 4.57 1c0 1.75-2.32 2.1-2.32 3.5M12 17h.01"
          ></path>
        </svg>
      </summary>
      <div class="help-content">${content}</div>
    </details>`;
  }

  private helpKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape") return;
    const help = event.currentTarget as HTMLDetailsElement;
    help.open = false;
    help.querySelector("summary")?.focus();
    event.stopPropagation();
  }

  private helpFocusOut(event: FocusEvent): void {
    const help = event.currentTarget as HTMLDetailsElement;
    if (!help.contains(event.relatedTarget as Node | null)) help.open = false;
  }

  private renderDummyLoadSection(stored?: DummyLoadSpec | null) {
    if (!supportsDummyLoad(this.meter)) return nothing;
    return renderDummyLoad({
      calibration: this.dummyLoadCalibration,
      stored,
      enabled: this.dummyLoadEnabled,
      mode: this.dummyLoadMode,
      voltageAvailable: hasVoltageReading(this.meter),
      onToggle: this.dummyLoadEnabledChanged,
      onModeChange: this.dummyLoadModeChanged,
    });
  }

  private dummyLoadEnabledChanged(event: Event): void {
    this.dummyLoadEnabled = (event.currentTarget as HTMLInputElement).checked;
    if (this.dummyLoadEnabled)
      this.dummyLoadMode = defaultDummyLoadMode(this.dummyLoadCalibration);
  }

  private dummyLoadModeChanged(event: Event): void {
    this.dummyLoadMode = (event.currentTarget as HTMLInputElement)
      .value as DummyLoadSpec["mode"];
  }

  /**
   * Advanced tuning section, built from the parameters the server says this type exposes.
   * A parameter that some option claims is shown only while that option is selected.
   */
  private renderTuning(
    definition: MeasureDefinition,
    request?: MeasurementRequest,
  ) {
    if (!this.capabilities) return nothing;
    const gated = gatedParameters(definition);
    const active = this.activeParameters(definition, request);
    const smartOn = this.parameterFlag("smart_sampling", request);
    const shown = definition.parameters.filter((parameter) => {
      if (gated.has(parameter.name) && !active.has(parameter.name))
        return false;
      if (
        (parameter.name === "smart_delta" ||
          parameter.name === "smart_border_delta" ||
          parameter.name === "smart_dart") &&
        !smartOn
      )
        return false;
      if (
        parameter.name === "smart_dart_min_delta" &&
        !this.parameterFlag("smart_dart", request)
      )
        return false;
      if (smartOn && CARTESIAN_RESOLUTION.has(parameter.name)) return false;
      return true;
    });
    const groups = groupParameters(shown);
    const hasRangeBounds = groups.some(
      (group) => group.name === RANGE_BOUNDS_GROUP,
    );
    return html`<details class="tuning-details" open>
      <summary>Timing & quality</summary>
      <div class="tuning-groups">
        ${groups.map((group) => {
          const sliders = group.parameters.filter(
            (parameter) =>
              parameter.axis && !(smartOn && SMART_SWEEPS.has(parameter.name)),
          );
          const others = group.parameters.filter(
            (parameter) =>
              !parameter.axis || (smartOn && SMART_SWEEPS.has(parameter.name)),
          );
          const slidersBlock = this.renderSliderFields(
            sliders,
            request,
            group.name === RANGE_BOUNDS_GROUP,
          );
          const estimateHere =
            group.name === RANGE_BOUNDS_GROUP ||
            (!hasRangeBounds && group.name === RESOLUTION_GROUP);
          return html`
            ${group.name
              ? html`<p class="advanced-heading">${group.name}</p>`
              : nothing}
            ${group.name === RANGE_BOUNDS_GROUP
              ? html`<div class="range-with-estimate">
                  ${this.renderEstimateSummary()}
                  <div class="range-sliders">${slidersBlock}</div>
                </div>`
              : html`${estimateHere
                  ? this.renderEstimateSummary()
                  : nothing}${slidersBlock}`}
            ${others.length
              ? html`<div class="grid">
                  ${others.map((parameter) =>
                    this.parameterField(parameter, request),
                  )}
                </div>`
              : nothing}
          `;
        })}
      </div>
    </details>`;
  }

  private renderSliderFields(
    parameters: MeasureParameter[],
    request: MeasurementRequest | undefined,
    pairRows: boolean,
  ) {
    if (!parameters.length) return nothing;
    const rows = pairRows
      ? parameters.reduce<MeasureParameter[][]>((chunks, parameter, index) => {
          if (index % 2 === 0) chunks.push([parameter]);
          else chunks[chunks.length - 1]?.push(parameter);
          return chunks;
        }, [])
      : [parameters];
    return html`${rows.map(
      (row) =>
        html`<div class="slider-grid">
          ${row.map((parameter) => this.parameterField(parameter, request))}
        </div>`,
    )}`;
  }

  private renderEstimateSummary() {
    if (this.selectedType !== "light") return nothing;
    if (this.estimateError)
      return html`<div class="estimate-summary" role="status">
        ${this.estimateError}
      </div>`;
    if (!this.estimate) return nothing;
    const estimate = this.estimate;
    return html`<div class="estimate-summary" aria-live="polite">
      ${estimate.modes.map(
        (mode) => html`<p>${sessionModeLabel(mode.mode)}: ${mode.summary}</p>`,
      )}
      <p>
        <strong>${estimate.total_points} LUT points</strong
        >${this.parameterFlag("smart_sampling")
          ? html` <span class="field-hint"
              >${this.parameterFlag("smart_dart")
                ? "(discovery now, then dart fill until you cancel)"
                : "(discovery now, coverage after the envelope is known)"}</span
            >`
          : nothing}
        ${estimate.remaining_points != null
          ? html` · ${estimate.remaining_points} remaining`
          : nothing}
      </p>
      ${estimate.total_readings
        ? html`<p>${estimate.total_readings} meter readings</p>`
        : nothing}
      ${estimate.estimated_duration_seconds != null
        ? html`<p>
            Estimated time: ${duration(estimate.estimated_duration_seconds)}
          </p>`
        : nothing}
      <p>Maximum time: ${duration(estimate.max_duration_seconds)}</p>
      ${estimate.estimated_from_runs
        ? html`<small class="field-hint"
            >${`Estimated from ${estimate.estimated_from_runs} previous ${estimate.estimated_from_runs === 1 ? "run" : "runs"} of this light and meter.`}</small
          >`
        : nothing}
      <small class="field-hint"
        >Maximum assumes every point waits the full settle time (no early
        plateau).</small
      >
      ${estimate.used_default_range
        ? html`<small class="field-hint"
            >Using the default color-temperature range until a light is
            selected.</small
          >`
        : nothing}
    </div>`;
  }

  private parameterField(
    parameter: MeasureParameter,
    request?: MeasurementRequest,
  ) {
    if (parameter.control === "boolean") {
      return html`<label class="check">
        <input
          type="checkbox"
          name=${parameter.name}
          .checked=${this.parameterFlag(parameter.name, request)}
          @change=${this.parameterFlagChanged}
        />
        ${parameter.label}
      </label>`;
    }
    const gate = parameter.requires_multiple;
    const allOn = parameter.all_values
      ? this.parameterFlag(parameter.all_values, request)
      : false;
    const bisectOn = parameter.bisection
      ? this.parameterFlag(parameter.bisection, request)
      : false;
    const { min: staticMin, max: staticMax } =
      this.capabilities?.limits?.[parameter.name] ?? {};
    const min = this.sliderMin(parameter, request, staticMin);
    const max = this.sliderMax(parameter, request, staticMax, staticMin);
    const smartOn = this.parameterFlag("smart_sampling", request);
    const sweepLabel = SMART_SWEEP_LABELS[parameter.name];
    const label =
      smartOn && sweepLabel
        ? sweepLabel
        : (bisectOn || allOn) && parameter.sweep_label
          ? parameter.sweep_label
          : parameter.label;
    const rawValue = this.parameterValue(parameter.name, request);
    const numeric = Number(rawValue);
    const inLiveBounds =
      parameter.axis === "kelvin" &&
      min != null &&
      max != null &&
      Number.isFinite(numeric);
    const displayValue =
      allOn && this.hasSelectedLight() && parameter.axis
        ? String(this.axisSpan(parameter.axis, request) ?? rawValue)
        : inLiveBounds
          ? String(Math.min(max, Math.max(min, numeric)))
          : rawValue;
    const snappedKelvin =
      parameter.axis === "kelvin" && Number.isFinite(Number(displayValue))
        ? snapKelvinToMired(Number(displayValue), this.kelvinMiredBounds())
        : null;
    const fieldValue = snappedKelvin
      ? String(snappedKelvin.kelvin)
      : displayValue;
    const detail = sliderSecondaryUnit(parameter, numeric, snappedKelvin);
    const disabled =
      allOn || (gate ? Number(this.parameterValue(gate, request)) <= 1 : false);
    const field = parameter.axis
      ? sliderNumberField(parameter.name, label, fieldValue, {
          min,
          max,
          step: parameter.step,
          hint: parameter.hint,
          detail,
          disabled,
          onInput: this.parameterChanged,
        })
      : numberField(parameter.name, label, displayValue, {
          min,
          max,
          step: parameter.step,
          hint: parameter.hint,
          disabled,
          onInput: this.parameterChanged,
        });
    if (!parameter.bisection && !parameter.all_values) {
      return allOn
        ? html`${field}<input
              type="hidden"
              name=${parameter.name}
              value=${this.parameterValue(parameter.name, request)}
            />`
        : field;
    }
    return html`<div class="resolution-field">
      <div class="resolution-field-row">
        ${field}
        ${parameter.bisection
          ? html`<label class="check">
              <input
                type="checkbox"
                name=${parameter.bisection}
                .checked=${bisectOn || allOn}
                ?disabled=${allOn}
                @change=${this.parameterFlagChanged}
              />
              Bisect
            </label>`
          : nothing}
        ${parameter.all_values
          ? html`<label class="check">
              <input
                type="checkbox"
                name=${parameter.all_values}
                .checked=${allOn}
                @change=${this.parameterFlagChanged}
              />
              All
            </label>`
          : nothing}
      </div>
      ${allOn
        ? html`<input
            type="hidden"
            name=${parameter.name}
            value=${this.parameterValue(parameter.name, request)}
          />`
        : nothing}
    </div>`;
  }

  /** What the field should show: what the user typed, else the previous run's, else the default. */
  private parameterValue(
    name: MeasureParameterName,
    request?: MeasurementRequest,
  ): string {
    const stored =
      request?.parameters[name] ?? this.capabilities?.defaults[name];
    return this.parameterValues[name] ?? String(stored ?? "");
  }

  private parameterChanged = (event: Event): void => {
    const input = event.currentTarget as HTMLInputElement;
    const name = input.name as MeasureParameterName;
    let value = input.value;
    if (name === "min_kelvin" || name === "max_kelvin") {
      const snapped = this.snapKelvinInput(
        Number(value),
        Number(this.parameterValue(name)),
      );
      if (snapped) {
        value = String(snapped.kelvin);
        input.value = value;
        const slider = input
          .closest(".slider-number")
          ?.querySelector<HTMLInputElement>('input[type="range"]');
        if (slider) slider.value = value;
      }
    }
    this.parameterValues = {
      ...this.parameterValues,
      [name]: value,
    };
  };

  private snapKelvinInput(
    kelvin: number,
    fromKelvin: number,
  ): { kelvin: number; mired: number } | null {
    const bounds = this.kelvinSliderBounds();
    if (
      !bounds ||
      !Number.isFinite(kelvin) ||
      kelvin < bounds.warm ||
      kelvin > bounds.cool
    ) {
      return null;
    }
    return stepKelvinToMired(kelvin, fromKelvin, this.kelvinMiredBounds());
  }

  private parameterFlag(
    name: MeasureParameterName,
    request?: MeasurementRequest,
  ): boolean {
    if (this.parameterFlags[name] !== undefined)
      return Boolean(this.parameterFlags[name]);
    const stored =
      request?.parameters[name] ?? this.capabilities?.defaults[name];
    return Boolean(stored);
  }

  private parameterFlagChanged = (event: Event): void => {
    const input = event.currentTarget as HTMLInputElement;
    this.parameterFlags = {
      ...this.parameterFlags,
      [input.name as MeasureParameterName]: input.checked,
    };
  };

  private hasSelectedLight(): boolean {
    return this.dummyController || this.selectedLightIds().length > 0;
  }

  private selectedLightIds(): string[] {
    const field = this.definition("light")?.fields.find(
      (candidate) => candidate.role === "controller",
    );
    return field ? this.selectedEntityIds(field, this.currentRun) : [];
  }

  private sliderMin(
    parameter: MeasureParameter,
    _request: MeasurementRequest | undefined,
    staticMin?: number,
  ): number | undefined {
    if (parameter.axis === "kelvin") {
      return this.kelvinSliderBounds()?.warm ?? staticMin;
    }
    return staticMin;
  }

  private sliderMax(
    parameter: MeasureParameter,
    request: MeasurementRequest | undefined,
    staticMax?: number,
    staticMin?: number,
  ): number | undefined {
    if (parameter.axis === "kelvin") {
      return this.kelvinSliderBounds()?.cool ?? staticMax;
    }
    if (
      parameter.name.startsWith("min_") ||
      parameter.name.startsWith("max_")
    ) {
      return parameter.axis === "hue" ? 65535 : 255;
    }
    if (parameter.axis && this.hasSelectedLight()) {
      const span = this.axisSpan(parameter.axis, request);
      const floor = staticMin ?? 1;
      if (span != null && span > floor) return span;
    }
    return staticMax;
  }

  /** Dummy LightInfo is 150–500 mired (2000–6666 K). */
  private lightMiredBounds(): { min: number; max: number } | undefined {
    if (this.dummyController) return { min: 150, max: 500 };
    const selected = this.selectedLightIds()
      .map((id) => this.lights.find((light) => light.entity_id === id))
      .filter((light): light is EntityDescriptor => Boolean(light));
    const mins = selected
      .map((light) => light.min_mired)
      .filter((value): value is number => value != null);
    const maxs = selected
      .map((light) => light.max_mired)
      .filter((value): value is number => value != null);
    if (!mins.length || !maxs.length) return undefined;
    const min = Math.max(...mins);
    const max = Math.min(...maxs);
    return max >= min ? { min, max } : undefined;
  }

  private lightKelvinBounds(): { warm: number; cool: number } | undefined {
    const mired = this.lightMiredBounds();
    if (!mired) return undefined;
    return { warm: miredToKelvin(mired.max), cool: miredToKelvin(mired.min) };
  }

  private staticMiredBounds(): { min: number; max: number } | undefined {
    const lo = this.capabilities?.limits?.min_kelvin?.min ?? 1500;
    const hi = this.capabilities?.limits?.max_kelvin?.max ?? 10000;
    if (!(lo > 0) || !(hi > 0)) return undefined;
    return { min: kelvinToMired(hi), max: kelvinToMired(lo) };
  }

  private kelvinMiredBounds(): { min: number; max: number } | undefined {
    return this.lightMiredBounds() ?? this.staticMiredBounds();
  }

  private kelvinSliderBounds(): { warm: number; cool: number } | undefined {
    const mired = this.kelvinMiredBounds();
    if (!mired) return undefined;
    return { warm: miredToKelvin(mired.max), cool: miredToKelvin(mired.min) };
  }

  private clampedMiredSpan(request?: MeasurementRequest): number | undefined {
    const light =
      this.lightMiredBounds() ??
      (this.hasSelectedLight() ? undefined : { min: 150, max: 500 });
    if (!light) return undefined;
    const minKelvin = Number(this.parameterValue("min_kelvin", request));
    const maxKelvin = Number(this.parameterValue("max_kelvin", request));
    let cool = light.min;
    let warm = light.max;
    if (
      Number.isFinite(minKelvin) &&
      Number.isFinite(maxKelvin) &&
      minKelvin > 0 &&
      maxKelvin > 0
    ) {
      cool = Math.max(cool, kelvinToMired(Math.max(minKelvin, maxKelvin)));
      warm = Math.min(warm, kelvinToMired(Math.min(minKelvin, maxKelvin)));
    }
    return warm >= cool ? warm - cool + 1 : undefined;
  }

  private axisSpan(
    axis: ResolutionAxis,
    request?: MeasurementRequest,
  ): number | undefined {
    if (axis === "mired") return this.clampedMiredSpan(request);
    if (axis === "kelvin") {
      const bounds = this.lightKelvinBounds();
      if (!bounds) return undefined;
      return bounds.cool - bounds.warm + 1;
    }
    const low =
      axis === "brightness"
        ? "min_brightness"
        : axis === "sat"
          ? "min_sat"
          : "min_hue";
    const high =
      axis === "brightness"
        ? "max_brightness"
        : axis === "sat"
          ? "max_sat"
          : "max_hue";
    const start = Number(this.parameterValue(low, request));
    const end = Number(this.parameterValue(high, request));
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start)
      return undefined;
    return end - start + 1;
  }

  private scheduleEstimate(): void {
    if (this.selectedType !== "light" || !this.loadEstimate) return;
    window.clearTimeout(this.estimateTimer);
    this.estimateTimer = window.setTimeout(() => {
      void this.refreshEstimate();
    }, ESTIMATE_DEBOUNCE_MS);
  }

  private async refreshEstimate(): Promise<void> {
    if (!this.loadEstimate || this.selectedType !== "light") return;
    const request = this.currentEstimateRequest();
    if (!request) {
      this.estimate = null;
      this.estimateError = "";
      return;
    }
    const generation = ++this.estimateGeneration;
    try {
      const result = await this.loadEstimate(request);
      if (generation !== this.estimateGeneration) return;
      this.estimate = result;
      this.estimateError = "";
    } catch (error) {
      if (generation !== this.estimateGeneration) return;
      this.estimateError =
        error instanceof Error && error.message
          ? error.message
          : "Could not estimate this grid.";
    }
  }

  private currentEstimateRequest(): MeasurementRequest | undefined {
    const definition = this.definition("light");
    const form = this.shadowRoot?.querySelector("form");
    if (!definition || !this.capabilities || !form) return undefined;
    const data = new FormData(form);
    this.syncMultiselectValues(form, data);
    this.syncModeCheckboxes(form, data);
    const dummy = this.dummyController || this.selectedLightIds().length === 0;
    return this.withLiveParameters(
      this.withRefineFields(
        buildMeasurementRequest(
          definition,
          data,
          this.capabilities,
          this.meter,
          this.defaultMeasureDevice,
          dummy,
        ),
      ),
    );
  }

  /** Prefer values the user just typed over FormData, which a re-render can reset. */
  private withLiveParameters(request: MeasurementRequest): MeasurementRequest {
    const parameters: Record<string, number | boolean> = {
      ...request.parameters,
    };
    for (const [name, value] of Object.entries(this.parameterValues)) {
      if (value === "") continue;
      const numeric = Number(value);
      if (Number.isFinite(numeric)) parameters[name] = numeric;
    }
    for (const [name, flag] of Object.entries(this.parameterFlags)) {
      if (flag !== undefined) parameters[name] = flag;
    }
    return {
      ...request,
      parameters: parameters as unknown as MeasurementRequest["parameters"],
    };
  }

  private genericField(
    field: MeasureDefinition["fields"][number],
    run?: MeasurementRequest,
  ) {
    if (!this.selectedType) return nothing;
    const definition = this.definition(this.selectedType);
    if (!definition) return nothing;
    if (!this.isFieldVisible(field, run)) return nothing;
    const name = field.name;
    if (this.dummyController && field.role === "controller") return nothing;
    if (field.derived_from) return this.derivedCountField(field, run);
    const stored = run && requestFieldValue(run, field);
    if (field.control === "boolean") {
      return html`<label class="check"
        ><input
          type="checkbox"
          name=${name}
          .checked=${Boolean(stored ?? field.default)}
        />${field.label}</label
      >`;
    }
    if (field.control === "entity") {
      const value = (stored ?? field.default ?? "").toString();
      const source = narrowingField(definition, field);
      const domains = source
        ? [
            entityDomain(definition, field, this.selectValue(source, run)),
          ].filter((domain): domain is string => Boolean(domain))
        : this.fieldDomains(field);
      // An all-entities field reads the "*" catalog; its declared domains only filter that
      // catalog client-side, so a stale error from another measure type must not surface here.
      const failed = field.all_entities
        ? this.deviceEntityErrors["*"]
          ? "*"
          : undefined
        : domains.find((domain) => this.deviceEntityErrors[domain]);
      if (failed) {
        return html`<div class="notice error" role="alert">
          Could not load ${field.label.toLowerCase()} entities:
          ${this.deviceEntityErrors[failed]}
        </div>`;
      }
      const entities = this.entityChoices(field, domains, run);
      if (
        field.multiple &&
        (field.role !== "controller" || this.multipleLights)
      ) {
        return this.multiEntityField(field, entities, run);
      }
      let selected = field.multiple
        ? this.selectedEntityId(field, run) || value
        : value;
      if (!selected && field.same_device_only && entities.length === 1)
        selected = entities[0]?.entity_id ?? "";
      const relatedMissing = Boolean(
        field.same_device_only &&
        this.relatedEntity(field, run) &&
        entities.length === 0,
      );
      const selector = entitySelect(name, field.label, entities, {
        selected,
        required: field.required,
        onChange: this.entityChanged,
      });
      if (!field.hint && !relatedMissing) return selector;
      return html`<div class="field-block">
        ${selector} ${fieldHint(field.hint ?? "")}
        ${relatedMissing
          ? html`<p class="notice error" role="alert">
              No usable battery percentage sensor was found on the same Home
              Assistant device. PowerCalc vacuum profiles require one; expose or
              add that sensor before recording.
            </p>`
          : nothing}
      </div>`;
    }
    if (field.control === "select") {
      const value =
        this.selectValue(field, run) ??
        (stored ?? field.default ?? "").toString();
      // Re-render when this select narrows another field, so that field's entities follow.
      const affectsAnother = definition.fields.some(
        (candidate) =>
          candidate.narrowed_by === name ||
          Object.hasOwn(candidate.visible_when ?? {}, name),
      );
      const selectedOption = field.options.find(
        (option) => option.value === value,
      );
      return html`<div class="field-block">
        ${optionSelect(name, field.label, field.options, {
          selected: value,
          required: field.required,
          onChange: affectsAnother ? this.selectChanged : null,
        })}${this.optionGuidance(selectedOption)}
      </div>`;
    }
    if (name === "export_filename" && this.selectedType === "recorder") {
      return this.valueField(
        field,
        recorderExportFilename(
          this.recorderPurpose(run),
          (stored ?? field.default ?? "").toString(),
        ),
      );
    }
    return this.valueField(field, (stored ?? field.default ?? "").toString());
  }

  private recorderPurpose(request?: MeasurementRequest): string | undefined {
    if (this.selectedType !== "recorder") return undefined;
    if (this.selectValues.recorder_purpose)
      return this.selectValues.recorder_purpose;
    if (request?.measure_type === "recorder") return request.recorder_purpose;
    return this.definition("recorder")
      ?.fields.find((field) => field.name === "recorder_purpose")
      ?.default?.toString();
  }

  private isFieldVisible(
    field: FormField,
    request?: MeasurementRequest,
  ): boolean {
    const definition = this.selectedType
      ? this.definition(this.selectedType)
      : undefined;
    return fieldVisible(field, (name) => {
      const source = definition?.fields.find(
        (candidate) => candidate.name === name,
      );
      if (!source) return "";
      if (source.control === "select")
        return this.selectValue(source, request) ?? "";
      const stored = request && requestFieldValue(request, source);
      return (
        this.selectedEntityId(source, request) ||
        (typeof stored === "string" ? stored : "")
      );
    });
  }

  private optionGuidance(option?: FormFieldOption) {
    if (!option?.description && !option?.guidance?.length) return nothing;
    const guidanceItems = option.guidance?.map(
      (item) => html`<li>${item}</li>`,
    );
    return html`<div class="select-guidance">
      ${option.description
        ? html`<p class="muted">${option.description}</p>`
        : nothing}
      ${guidanceItems?.length
        ? html`<ul>
            ${guidanceItems}
          </ul>`
        : nothing}
    </div>`;
  }

  /** A plain text or number input, rendered from what the field declares about itself. */
  private valueField(
    field: FormField,
    value: string,
    onInput: ((event: Event) => void) | null = null,
  ) {
    return html`<label
      ><span>${field.label}</span
      ><input
        type=${field.control === "number" ? "number" : "text"}
        name=${field.name}
        min=${field.minimum ?? nothing}
        max=${field.maximum ?? nothing}
        step=${field.control === "number" ? (field.step ?? nothing) : nothing}
        .value=${value}
        ?required=${field.required}
        autocomplete="off"
        @input=${onInput}
      />${field.hint
        ? html`<small class="field-hint">${field.hint}</small>`
        : nothing}</label
    >`;
  }

  /**
   * A count that follows how many entities its source field selects. It only means anything
   * while several devices are measured together, so until then it submits a fixed 1 out of sight.
   */
  private derivedCountField(field: FormField, run?: MeasurementRequest) {
    if (!this.multipleLights)
      return html`<input type="hidden" name=${field.name} value="1" />`;
    const source = this.selectedType
      ? this.definition(this.selectedType)?.fields.find(
          (candidate) => candidate.name === field.derived_from,
        )
      : undefined;
    const derived = source ? this.physicalLightCount(source, run) : 0;
    const stored = run && requestFieldValue(run, field);
    const value =
      this.derivedCountOverride ??
      (derived > 1
        ? String(derived)
        : (stored ?? field.default ?? "").toString());
    return html`<div class="field-with-help">
      ${this.valueField(
        { ...field, hint: undefined },
        value,
        this.derivedCountChanged,
      )}
      ${this.contextHelp(
        field.label,
        html`<p>
          Total number of identical physical lights, including all members of a
          group. Measured power is divided by this value to calculate power per light.
        </p>`,
      )}
    </div>`;
  }

  /** Unique leaf lights, walking HA group membership so a 5-bulb group counts as 5. */
  private physicalLightCount(
    source: FormField,
    run?: MeasurementRequest,
  ): number {
    const ids = this.selectedEntityIds(source, run);
    const entities = [
      ...this.lights,
      ...Object.values(this.deviceEntities).flat(),
    ];
    const byId = new Map(
      entities.map((entity) => [entity.entity_id, entity] as const),
    );
    const leaves = new Set<string>();
    const seen = new Set<string>();
    const walk = (entityId: string): void => {
      if (seen.has(entityId)) return;
      seen.add(entityId);
      const members = byId.get(entityId)?.member_entity_ids ?? [];
      if (members.length) members.forEach(walk);
      else leaves.add(entityId);
    };
    ids.forEach(walk);
    return leaves.size;
  }

  private derivedCountChanged(event: Event): void {
    this.derivedCountOverride = (event.currentTarget as HTMLInputElement).value;
  }

  private multiEntityField(
    field: FormField,
    entities: EntityDescriptor[],
    run?: MeasurementRequest,
  ) {
    if (this.selectedType === "light" && field.role === "controller") {
      return html`<measure-combobox
        name=${field.name}
        label=${field.plural_label || field.label}
        .value=${this.selectedEntityIds(field, run)}
        .options=${entities.map((entity) => ({
          value: entity.entity_id,
          label: `${entity.name} · ${entity.entity_id}`,
        }))}
        placeholder="Select lights"
        ?required=${field.required}
        multiple
        @combobox-change=${(event: CustomEvent<{ value: string[] }>) =>
          this.selectEntities(field.name, event.detail.value)}
      ></measure-combobox>`;
    }
    return renderEntityList({
      field,
      entities,
      rows: this.entityRows(field, run),
      onChange: (rows) => this.selectEntities(field.name, rows),
    });
  }

  private fieldDomains(field: MeasureDefinition["fields"][number]): string[] {
    return field.entity_domains ?? [];
  }

  private multiSelectField(field: FormField, request?: MeasurementRequest) {
    const selected = this.selectedOptions(field, request);
    const brightnessImplied =
      field.name === "modes" && colorModesCoverBrightness(selected);
    return html`
      <fieldset>
        <legend>
          ${this.selectedType === "light" && field.name === "modes"
            ? "What do you want to measure?"
            : field.label}
        </legend>
        <div class="checks">
          ${this.availableOptions(field, request).map((option) => {
            const implied = brightnessImplied && option.value === "brightness";
            return html`
              <label class="check">
                <input
                  type="checkbox"
                  name=${field.name}
                  value=${option.value}
                  .checked=${implied || selected.includes(option.value)}
                  ?disabled=${implied}
                  @change=${this.multiSelectChanged(field)}
                />
                ${option.label}
              </label>
            `;
          })}
        </div>
        ${brightnessImplied
          ? html`<small class="field-hint"
              >Color temperature and hue already sweep the full brightness
              range, so a separate brightness pass is not run.</small
            >`
          : nothing}
      </fieldset>
    `;
  }

  private multiSelectChanged(field: FormField): () => void {
    return () => {
      const boxes = [
        ...(this.shadowRoot?.querySelectorAll<HTMLInputElement>(
          `input[name="${field.name}"]`,
        ) ?? []),
      ];
      this.multiSelection = {
        ...this.multiSelection,
        [field.name]: boxes
          .filter((box) => box.checked)
          .map((box) => box.value),
      };
    };
  }

  private dummyControllerChanged(event: Event): void {
    this.dummyController = (event.currentTarget as HTMLInputElement).checked;
  }

  private multipleLightsChanged(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    this.multipleLights = input.checked;
    if (this.multipleLights) return;
    // Back to one light: keep the first pick so the single selector stays populated, and let the count be derived again.
    const field = input.dataset.field ?? "";
    this.selectEntities(
      field,
      this.currentRows(field).filter(Boolean).slice(0, 1),
    );
    this.derivedCountOverride = undefined;
  }

  /** Options a field offers right now, narrowed by the capabilities of the entities it names. */
  private availableOptions(
    field: FormField,
    request?: MeasurementRequest,
  ): FormFieldOption[] {
    // A virtual device stands in for any real one, so it supports everything on offer.
    if (this.dummyController) return field.options;
    return fieldOptions(field, this.narrowedModes(field, request));
  }

  /** Currently selected values: what the user picked, else the previous run's, else everything offered. */
  private selectedOptions(
    field: FormField,
    request?: MeasurementRequest,
  ): string[] {
    const available = this.availableOptions(field, request).map(
      (option) => option.value,
    );
    const stored = request && requestFieldValue(request, field);
    const chosen =
      this.multiSelection[field.name] ??
      (Array.isArray(stored) && stored.length ? stored : available);
    return available.filter((value) => chosen.includes(value));
  }

  /** Parameters the selected options activate; every other parameter stays disabled. */
  private activeParameters(
    definition: MeasureDefinition,
    request?: MeasurementRequest,
  ): ReadonlySet<string> {
    const active = new Set<string>();
    for (const field of definition.fields.filter(
      (candidate) => candidate.control === "multi_select",
    )) {
      for (const name of enabledParameters(
        field,
        this.selectedOptions(field, request),
      ))
        active.add(name);
    }
    return active;
  }

  /** Modes every entity named by this field's narrowing source supports; undefined when none is selected. */
  private narrowedModes(
    field: FormField,
    request?: MeasurementRequest,
  ): LutMode[] | undefined {
    const definition = this.selectedType
      ? this.definition(this.selectedType)
      : undefined;
    const source = field.narrowed_by
      ? definition?.fields.find(
          (candidate) => candidate.name === field.narrowed_by,
        )
      : undefined;
    if (!source) return undefined;
    const choices = this.entityChoices(source);
    const selected = this.selectedEntityIds(source, request)
      .map((entityId) =>
        choices.find((entity) => entity.entity_id === entityId),
      )
      .filter((entity): entity is EntityDescriptor => Boolean(entity));
    const [first, ...rest] = selected;
    if (!first) return undefined;
    return (first.supported_modes ?? []).filter((mode) =>
      rest.every((entity) => entity.supported_modes?.includes(mode)),
    );
  }

  /** Entities offered for a controller field. Lights arrive with the startup catalog, other domains on demand. */
  private entityChoices(
    field: FormField,
    domains = this.fieldDomains(field),
    request?: MeasurementRequest,
  ): EntityDescriptor[] {
    let entities = field.all_entities
      ? [...(this.deviceEntities["*"] ?? [])]
      : this.entitiesIn(domains);
    if (field.all_entities && domains.length) {
      entities = entities.filter(
        (entity) => entity.domain && domains.includes(entity.domain),
      );
    }
    if (field.entity_device_classes?.length) {
      entities = entities.filter((entity) =>
        this.matchesDeviceClass(entity, field.entity_device_classes ?? []),
      );
    }
    const related = this.relatedEntity(field, request);
    if (!related?.device_id) return field.same_device_only ? [] : entities;
    if (field.same_device_only)
      return entities.filter(
        (entity) => entity.device_id === related.device_id,
      );
    return entities.sort(
      (left, right) =>
        Number(right.device_id === related.device_id) -
        Number(left.device_id === related.device_id),
    );
  }

  private matchesDeviceClass(
    entity: EntityDescriptor,
    deviceClasses: readonly string[],
  ): boolean {
    if (!entity.device_class || !deviceClasses.includes(entity.device_class))
      return false;
    if (entity.device_class !== "battery") return true;
    return (
      entity.domain === "sensor" &&
      entity.unit === "%" &&
      !["unavailable", "unknown", "none"].includes(
        (entity.state ?? "").toLowerCase(),
      ) &&
      Number.isFinite(Number(entity.state))
    );
  }

  private relatedEntity(
    field: FormField,
    request?: MeasurementRequest,
  ): EntityDescriptor | undefined {
    if (!field.related_to || !this.selectedType) return undefined;
    const source = this.definition(this.selectedType)?.fields.find(
      (candidate) => candidate.name === field.related_to,
    );
    if (!source) return undefined;
    const entityId = this.selectedEntityId(source, request);
    return (this.deviceEntities["*"] ?? []).find(
      (entity) => entity.entity_id === entityId,
    );
  }

  private entitiesIn(domains: string[]): EntityDescriptor[] {
    return domains.flatMap((domain) =>
      domain === "light" ? this.lights : (this.deviceEntities[domain] ?? []),
    );
  }

  /**
   * Rows a field currently shows: what the user picked, else what a previous run stored.
   * Empty rows are kept, because an unanswered select is still a row in the form.
   */
  private entityRows(field: FormField, request?: MeasurementRequest): string[] {
    const chosen = this.selectedEntities[field.name];
    if (chosen) return chosen;
    const stored = request && requestFieldValue(request, field);
    if (Array.isArray(stored)) return stored.map(String);
    return typeof stored === "string" && stored ? [stored] : [];
  }

  private selectedEntityId(
    field: FormField,
    request?: MeasurementRequest,
  ): string {
    return this.entityRows(field, request)[0] ?? "";
  }

  private selectedEntityIds(
    field: FormField,
    request?: MeasurementRequest,
  ): string[] {
    return this.entityRows(field, request).filter(Boolean);
  }

  private readonly selectType = (type: MeasureType): void => {
    this.errorMessage = "";
    this.selectedType = type;
    this.dummyController = false;
    this.multipleLights = false;
    emit<MeasureType>(this, "measure-type-selected", type);
  };

  private readonly changeType = (): void => {
    this.errorMessage = "";
    this.selectedType = undefined;
  };

  private entityChanged(event: Event): void {
    const select = event.currentTarget as HTMLInputElement;
    this.selectEntities(select.name, [select.value]);
    const definition = this.selectedType
      ? this.definition(this.selectedType)
      : undefined;
    for (const dependent of definition?.fields.filter(
      (field) => field.related_to === select.name,
    ) ?? []) {
      this.selectEntities(dependent.name, []);
    }
  }

  /** Rows as the form currently shows them, so an edit starts from what the user can see. */
  private currentRows(name: string): string[] {
    const field = this.selectedType
      ? this.definition(this.selectedType)?.fields.find(
          (candidate) => candidate.name === name,
        )
      : undefined;
    return field ? this.entityRows(field, this.currentRun) : [];
  }

  /** The previous run, when it belongs to the type now being configured. */
  private get currentRun(): MeasurementRequest | undefined {
    return this.initialRequest?.measure_type === this.selectedType
      ? this.initialRequest
      : undefined;
  }

  /** The controller field that accepts several entities at once, when this type has one. */
  private multiControllerField(): FormField | undefined {
    const definition = this.selectedType
      ? this.definition(this.selectedType)
      : undefined;
    return definition?.fields.find(
      (field) => field.role === "controller" && field.multiple,
    );
  }

  private selectEntities(name: string, rows: string[]): void {
    this.selectedEntities = { ...this.selectedEntities, [name]: rows };
  }

  private readonly openSettings = (): void => {
    emit(this, "open-settings");
  };

  /**
   * Catch an entity that does not match the domain its narrowing field currently calls for —
   * stale options can survive a change of that field until the entity list reloads.
   */
  private narrowedEntityMismatch(
    definition: MeasureDefinition,
    form: FormData,
  ): string | undefined {
    for (const field of definition.fields) {
      const source = narrowingField(definition, field);
      if (!source || field.role !== "controller") continue;
      const expected = entityDomain(
        definition,
        field,
        formText(form, source.name),
      );
      const chosen = formText(form, field.name);
      if (!expected || !chosen.startsWith(`${expected}.`)) {
        return `Select a ${expected ?? "matching"} entity for the chosen ${source.label.toLowerCase()}.`;
      }
    }
    return undefined;
  }

  private selectChanged(event: Event): void {
    const select = event.currentTarget as HTMLInputElement;
    this.selectValues = { ...this.selectValues, [select.name]: select.value };
    const definition = this.selectedType
      ? this.definition(this.selectedType)
      : undefined;
    const form = select.closest("form");
    if (definition)
      emit<string[]>(
        this,
        "entity-domains-requested",
        entityDomains(definition, form ? new FormData(form) : undefined),
      );
  }

  /** Value a narrowing select currently holds: the user's choice, else the previous run's, else its first option. */
  private selectValue(
    field: FormField,
    request?: MeasurementRequest,
  ): string | undefined {
    const stored = request && requestFieldValue(request, field);
    return (
      this.selectValues[field.name] ??
      (typeof stored === "string" ? stored : undefined) ??
      field.options[0]?.value
    );
  }

  private definition(type: MeasureType): MeasureDefinition | undefined {
    return this.definitions.find((item) => item.measure_type === type);
  }

  /** Lights with nothing to confirm start at once; dummy-load and warned types still review. */
  private startsWithoutReview(type: MeasureType): boolean {
    if (this.dummyLoadEnabled) return false;
    const definition = this.definition(type);
    return (
      !definition?.confirmation_action && !definition?.confirmation_is_warning
    );
  }

  /** Only shared device metadata is safe to use as profile defaults for a multi-device run. */
  private profileDefaults(): {
    model_id: string;
    product_name: string;
    session_name: string;
  } {
    const empty = { model_id: "", product_name: "", session_name: "" };
    if (this.dummyController) return empty;
    const controller = this.selectedType
      ? this.definition(this.selectedType)?.fields.find(
          (field) => field.role === "controller",
        )
      : undefined;
    if (!controller) return empty;
    const entities = [
      ...this.lights,
      ...Object.values(this.deviceEntities).flat(),
    ];
    const ids = this.selectedEntityIds(controller, this.initialRequest);
    const selected = ids.map((id) =>
      entities.find((entity) => entity.entity_id === id),
    );
    const shared = (field: "model_id" | "product_name") => {
      const values = new Set(selected.map((entity) => entity?.[field]));
      return values.size === 1 ? ([...values][0] ?? "") : "";
    };
    const modelId = shared("model_id");
    return {
      // An HA model ID can contain characters not allowed in an export path.
      // Leave those for Prepare, which also resolves the original ID from HA.
      model_id:
        modelId.length <= 120 &&
        /^[A-Za-z0-9][A-Za-z0-9 ._()+-]*$/.test(modelId)
          ? modelId
          : "",
      product_name: shared("product_name"),
      session_name: selected.length === 1 ? (selected[0]?.name ?? "") : "",
    };
  }

  private submitMeasurement(event: SubmitEvent): void {
    event.preventDefault();
    const definition = this.selectedType
      ? this.definition(this.selectedType)
      : undefined;
    if (!definition || !this.capabilities) return;
    const formElement = event.currentTarget as HTMLFormElement;
    const form = new FormData(formElement);
    this.syncMultiselectValues(formElement, form);
    this.syncModeCheckboxes(formElement, form);
    const failedDomain = this.failedEntityDomain(definition, form);
    if (failedDomain) {
      this.errorMessage = `Could not load ${failedDomain} entities. Retry before starting the measurement.`;
      return;
    }
    // A checkbox group submits nothing at all when it is empty, so require it here.
    const empty = this.missingRequiredMultiselect(definition, form);
    if (empty) {
      this.errorMessage = `Select at least one ${empty.label.toLowerCase().replace(/s$/, "")}.`;
      return;
    }
    const request = this.withRefineFields(
      buildMeasurementRequest(
        definition,
        form,
        this.capabilities,
        this.meter,
        this.defaultMeasureDevice,
        this.dummyController,
      ),
    );
    const defaults = this.profileDefaults();
    const previous = this.previousRequest(definition, request);
    request.model_id = previous?.model_id || defaults.model_id;
    request.product_name = previous?.product_name || defaults.product_name;
    request.session_name ||= defaults.session_name;
    request.dummy_load = supportsDummyLoad(this.meter)
      ? dummyLoadSpec(form, this.dummyLoadCalibration)
      : undefined;
    const mismatch = this.dummyController
      ? undefined
      : this.narrowedEntityMismatch(definition, form);
    if (mismatch) {
      this.errorMessage = mismatch;
      return;
    }
    emit<MeasurementRequest>(this, "preflight", request);
  }

  private failedEntityDomain(
    definition: MeasureDefinition,
    form: FormData,
  ): string | undefined {
    if (this.dummyController) return undefined;
    return entityDomains(definition, form).find(
      (domain) => this.deviceEntityErrors[domain],
    );
  }

  private missingRequiredMultiselect(
    definition: MeasureDefinition,
    form: FormData,
  ): FormField | undefined {
    return definition.fields.find(
      (field) =>
        field.control === "multi_select" &&
        field.required &&
        form.getAll(field.name).length === 0,
    );
  }

  private previousRequest(
    definition: MeasureDefinition,
    request: MeasurementRequest,
  ): MeasurementRequest | undefined {
    if (this.initialRequest?.measure_type !== definition.measure_type)
      return undefined;
    if (
      JSON.stringify(this.initialRequest.controller) !==
      JSON.stringify(request.controller)
    )
      return undefined;
    return this.initialRequest;
  }

  /** Enabled, checked LUT modes only. Implied brightness is not submitted. */
  private syncModeCheckboxes(formElement: HTMLFormElement, form: FormData): void {
    const boxes = [
      ...formElement.querySelectorAll<HTMLInputElement>('input[name="modes"]'),
    ];
    if (!boxes.length) return;
    const selected = boxes
      .filter((box) => box.checked && !box.disabled)
      .map((box) => box.value);
    form.delete("modes");
    for (const mode of selected) form.append("modes", mode);
  }

  /** Read multiselects explicitly for environments without form-associated custom elements. */
  private syncMultiselectValues(
    formElement: HTMLFormElement,
    form: FormData,
  ): void {
    for (const picker of formElement.querySelectorAll<Combobox>(
      "measure-combobox[multiple]",
    )) {
      form.delete(picker.name);
      if (picker.disabled || !Array.isArray(picker.value)) continue;
      for (const value of picker.value) form.append(picker.name, value);
    }
  }

  private meterContext(): MeterContext {
    return { powers: this.powers, voltages: this.voltages };
  }
}

/**
 * The export filename a recorder purpose implies. Both the server and the result plotter
 * pick the format by extension, so the name follows the purpose rather than whatever a
 * duplicated session left behind. An unrecognised extension is the user's own, and stays.
 */
function groupParameters(
  parameters: MeasureParameter[],
): { name: string; parameters: MeasureParameter[] }[] {
  const groups: { name: string; parameters: MeasureParameter[] }[] = [];
  for (const parameter of parameters) {
    const name = parameter.group ?? "";
    const current = groups.at(-1);
    if (current && current.name === name) current.parameters.push(parameter);
    else groups.push({ name, parameters: [parameter] });
  }
  return groups;
}

export function recorderExportFilename(
  purpose: string | undefined,
  name: string,
): string {
  const wanted = purpose === "complex_profile" ? "jsonl" : "csv";
  if (!name) return `record.${wanted}`;
  const dot = name.lastIndexOf(".");
  const current = dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
  if (current !== "csv" && current !== "jsonl") return name;
  return current === wanted ? name : `${name.slice(0, dot)}.${wanted}`;
}
