import { LitElement, css, html, nothing, svg, type PropertyValues } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import { createRef, ref } from "lit/directives/ref.js";
import type { AppSettings, AppSettingsUpdate, Capabilities, ContributionAuthDeviceStatus, ContributionAuthState, ContributionDeviceFlow, EntityDescriptor, MeasureParameterName, PowerMeterDiagnostic, PowerMeterType, SettingsSection, ShellyDiscoveryDevice, WitnessMeterSettings, WitnessPosition, WitnessSettings } from "../types";
import { DEFAULT_SHELLY_USERNAME, POWER_METER_LIST, meterFor, settingsFromForm } from "../power-meter";

/** A meter type a witness can use. Every meter the app itself supports is valid here too —
 * a witness reads the same way a primary would, just to cross-check it rather than replace it.
 * `manual` is excluded app-wide (see `_SINGLE_METER_SPEC_BUILDERS` in `ha_app/api.py`) since it
 * blocks on a console prompt the background worker has no console to answer. */
const WITNESS_METER_TYPES: PowerMeterType[] = POWER_METER_LIST.map((meter) => meter.type).filter((type) => type !== "dummy");

function newWitness(): WitnessSettings {
  return { meter: { type: "shelly" }, position: "none", offset_w: 0, tolerance_w: 0.5, tolerance_pct: 2.0, required: true };
}

/** Options for the witness-position picker, in wiring order (grid → primary → device). Order
 * matches how Johan described them: "does not affect primary", "after primary", "before primary". */
const WITNESS_POSITION_OPTIONS: { value: WitnessPosition; label: string; hint: string }[] = [
  {
    value: "none",
    label: "Doesn't affect the primary",
    hint: "Not electrically in line with the primary at all (e.g. a current clamp on the same wire). The compensation below, if any, is just this witness's own fixed calibration bias and never touches the primary's recorded reading — it can be negative if the witness reads low rather than high.",
  },
  {
    value: "after_primary",
    label: "Between the primary and the device",
    hint: "Closer to the device than the primary is. The primary also sees this witness's own self-consumption as if it were part of the device's draw, so the compensation below is subtracted from the primary's recorded reading too, not just used for the agreement check.",
  },
  {
    value: "before_primary",
    label: "Between the grid and the primary",
    hint: "Farther from the device than the primary is. The primary already reads the device correctly by itself; the compensation below only corrects this witness so it can agree with the primary, and never changes what gets recorded.",
  },
];
import { formRaw, formText, formTextOrNull } from "../form";
import { emit } from "../events";
import { sharedStyles } from "../styles";
import type { ComboboxOption } from "./combobox";
import "./combobox";
import { optionSelect } from "./fields";
import "./power-meter-diagnostic";

interface SettingsSectionDescriptor {
  id: SettingsSection;
  label: string;
  icon: () => unknown;
}

const icon = (path: unknown) => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;

/** The sections of the settings screen, in the order the navigation lists them. */
const SETTINGS_SECTIONS: SettingsSectionDescriptor[] = [
  {
    id: "power_meter",
    label: "Power meter",
    icon: () => icon(svg`<path d="M13 2 5.5 13h6L11 22l7.5-11h-6L13 2Z"></path>`),
  },
  {
    id: "profile",
    label: "Profile metadata",
    icon: () => icon(svg`<path d="M4 4h16v16H4z"></path><path d="M8 9h8M8 13h8M8 17h5"></path>`),
  },
  {
    id: "measure_tuning",
    label: "Measure tuning",
    icon: () => icon(svg`
      <path d="M4 7h10M18 7h2M4 17h2M10 17h10"></path>
      <circle cx="16" cy="7" r="2"></circle>
      <circle cx="8" cy="17" r="2"></circle>
    `),
  },
  {
    id: "github",
    label: "GitHub",
    icon: () => icon(svg`<path d="M9 19c-4.2 1.2-4.2-2-6-2.4M15 22v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.7 4.7 0 0 0-1.3-3.3 4.4 4.4 0 0 0-.1-3.2s-1-.3-3.4 1.3a11.8 11.8 0 0 0-6.2 0C6.6 3.7 5.6 4 5.6 4a4.4 4.4 0 0 0-.1 3.2A4.7 4.7 0 0 0 4.2 10.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V22"></path>`),
  },
];

@customElement("measure-settings-view")
export class SettingsView extends LitElement {
  @property({ attribute: false })
  powers: EntityDescriptor[] = [];

  @property({ attribute: false })
  settings?: AppSettings;

  @property({ attribute: false })
  capabilities?: Capabilities;

  @property({ attribute: false })
  measureDevices: string[] = [];

  @property({ type: Boolean })
  measureDevicesLoading = false;

  @property({ type: String })
  measureDevicesError = "";

  @state()
  meter?: PowerMeterType;

  @property({ type: Boolean })
  busy = false;

  @property({ type: Boolean })
  testing = false;

  @property({ attribute: false })
  testResult?: PowerMeterDiagnostic;

  @property({ type: String })
  errorMessage = "";

  @state()
  activeSection: SettingsSection = "power_meter";

  @property({ attribute: false })
  initialSection?: SettingsSection;

  private appliedInitialSection = false;

  @property({ attribute: false })
  contributionAuth?: ContributionAuthState;

  @property({ attribute: false })
  contributionDeviceFlow?: ContributionDeviceFlow;

  @property({ attribute: false })
  contributionDeviceStatus?: ContributionAuthDeviceStatus;

  @property({ type: Boolean })
  contributionAuthBusy = false;

  @property({ type: String })
  contributionAuthError = "";

  @state()
  private githubCopyStatus = "";

  @state()
  private contributorGithubValue?: string;

  @property({ attribute: false })
  shellyDiscoveryDevices: ShellyDiscoveryDevice[] = [];

  @property({ type: Boolean })
  discoveringShellys = false;

  @property({ type: String })
  shellyDiscoveryError = "";

  @property({ attribute: false })
  shellyDiscoveryAvailable?: boolean;

  @property({ attribute: false })
  shellyDiscoveryMessage?: string | null;

  @state()
  private shellyIp?: string;

  @state()
  private shellyUsername?: string;

  @state()
  private shellyPassword = "";

  @state()
  private clearShellyPassword = false;

  @state()
  private kasaIp?: string;

  /** Local editable copy of the configured witnesses; kept in state (not read from `settings`
   * directly at render time) for the same reason `meter`/`shellyIp`/etc. are — so typing in a
   * row survives an app-shell re-render while the form is still open. */
  @state()
  private witnesses: WitnessSettings[] = [];

  private witnessesInitialized = false;

  private readonly form = createRef<HTMLFormElement>();

  @state()
  private measureDeviceValue = "";

  @state()
  private hassPowerEntity = "";

  static readonly styles = [sharedStyles, css`
    :host { display: block; min-width: 0; max-width: 100%; }
    form { display: grid; gap: 1rem; min-width: 0; max-width: 100%; margin-top: 1rem; }
    .settings-layout { display: grid; grid-template-columns: minmax(180px, 0.32fr) minmax(0, 1fr); gap: 1.25rem; align-items: start; }
    .settings-nav { display: grid; gap: 0.4rem; padding: 0.45rem; border: 1px solid var(--line); border-radius: 12px; background: var(--field); }
    .settings-nav button { display: grid; grid-template-columns: 24px 1fr; align-items: center; gap: 0.65rem; min-height: 48px; padding: 0.65rem 0.75rem; border-color: transparent; background: transparent; text-align: left; }
    .settings-nav button:hover { border-color: var(--line); }
    .settings-nav button.active { border-color: var(--signal); background: color-mix(in srgb, var(--signal) 13%, transparent); color: var(--signal-strong); }
    .nav-icon { display: grid; place-items: center; width: 24px; height: 24px; }
    .nav-icon svg { width: 20px; height: 20px; }
    .settings-section { min-width: 0; padding: 1rem 1.1rem 1.2rem; border: 1px solid var(--line); border-radius: 12px; }
    .settings-section h3 { margin: 0 0 0.35rem; color: var(--ink); font-size: 1rem; }
    .settings-section > .muted { margin: 0 0 1rem; }
    .section-fields { display: grid; gap: 1rem; }
    .check { align-items: flex-start; gap: 0.6rem; }
    .check input { margin-top: 0.2rem; }
    .developer-option { margin-bottom: 1rem; padding: 0.85rem; border: 1px solid var(--signal); border-radius: 10px; background: color-mix(in srgb, var(--signal) 8%, transparent); }
    .developer-option strong { color: var(--ink); }
    .quality-requirements { margin: -0.15rem 0 0; padding: 0.7rem 0.8rem; border-left: 3px solid var(--signal); background: color-mix(in srgb, var(--signal) 8%, transparent); color: var(--muted); font-size: 0.76rem; line-height: 1.45; }
    .witnesses { padding-top: 0.85rem; border-top: 1px solid var(--line); }
    .witnesses h4 { margin: 0 0 0.35rem; color: var(--ink); font-size: 0.9rem; }
    .witness-row { display: grid; gap: 0.65rem; padding: 0.75rem 0.8rem; margin-bottom: 0.75rem; border: 1px solid var(--line); border-radius: 10px; background: color-mix(in srgb, var(--field) 60%, transparent); }
    .witness-row-header { display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; }
    .witness-row-header strong { color: var(--ink); font-size: 0.82rem; }
    .witness-position { display: grid; gap: 0.5rem; padding: 0.6rem 0.7rem; margin: 0; border: 1px solid var(--line); border-radius: 8px; }
    .witness-position legend { padding: 0 0.3rem; color: var(--muted); font-size: 0.76rem; font-weight: 650; }
    .witness-position-option { display: flex; gap: 0.55rem; align-items: flex-start; cursor: pointer; }
    .witness-position-option input { margin-top: 0.2rem; flex: none; }
    .witness-position-option strong { display: block; font-size: 0.82rem; }
    .witness-position-option .field-hint { margin-top: 0.1rem; }
    .test-row { display: grid; gap: 0.75rem; }
    .test-row > button { justify-self: start; }
    .test-row button { min-height: 40px; }
    .discovery { display: grid; gap: 0.65rem; padding: 0.8rem; border: 1px solid var(--line); border-radius: 10px; background: color-mix(in srgb, var(--field) 68%, transparent); }
    .discovery-header { display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; }
    .discovery-header strong { color: var(--ink); font-size: 0.82rem; }
    .discovery-header button { min-height: 36px; padding: 0.45rem 0.7rem; }
    .discovery-status { margin: 0; color: var(--muted); font-size: 0.76rem; line-height: 1.45; }
    .discovery-status.error { color: var(--danger); }
    .github-card { display: grid; gap: 0.8rem; padding: 0.85rem; border: 1px solid var(--line); border-radius: 10px; background: color-mix(in srgb, var(--field) 70%, transparent); }
    .github-card > button, .device-flow > button { justify-self: start; }
    .identity { display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
    .identity strong, .device-code { color: var(--ink); }
    .device-flow { display: grid; gap: 0.85rem; }
    .device-step { display: grid; gap: 0.45rem; }
    .device-step p { margin: 0; }
    .device-code-row { display: grid; grid-template-columns: minmax(0, 220px) auto; gap: 0.65rem; align-items: center; }
    .device-code {
      width: 100%; min-height: 44px; padding: 0.65rem 0.75rem; border: 1px solid var(--line); border-radius: 9px;
      background: var(--canvas); font: 700 1.2rem/1 ui-monospace, monospace; letter-spacing: 0.12em; text-align: center;
    }
    .github-link {
      justify-self: start; min-height: 44px; padding: 0.65rem 0.85rem; border: 1px solid var(--signal); border-radius: 9px;
      display: inline-flex; align-items: center; background: var(--signal); color: var(--on-signal); font-weight: 750; text-decoration: none;
    }
    .github-link:hover { filter: brightness(1.08); }
    .token-fallback summary { cursor: pointer; color: var(--ink); font-weight: 700; }
    .token-fallback label { margin-top: 0.8rem; }
    .token-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0.65rem; align-items: end; }
    @media (max-width: 700px) {
      .settings-layout { grid-template-columns: 1fr; }
      .settings-nav { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    }
    @media (max-width: 520px) {
      .token-row, .device-code-row { grid-template-columns: 1fr; }
      .settings-nav { grid-template-columns: 1fr; }
    }
  `];

  willUpdate(changedProperties: PropertyValues<this>) {
    if (changedProperties.has("settings")) {
      this.measureDeviceValue = this.settings?.default_measure_device ?? "";
      this.hassPowerEntity = this.settings?.default_power_entity_id ?? "";
      this.contributorGithubValue = undefined;
      // Only seed from settings once real settings arrive: after that, this.witnesses is the
      // source of truth so an in-progress edit (a row half-filled in) survives a re-fetch.
      if (!this.witnessesInitialized && this.settings) {
        this.witnesses = (this.settings.witnesses ?? []).map((witness) => structuredClone(witness));
        this.witnessesInitialized = true;
      }
    }
    // Honour a requested section (e.g. opened from the GitHub contribution shortcut) once,
    // while still letting the user switch sections afterwards.
    if (!this.appliedInitialSection && this.initialSection) {
      this.activeSection = this.initialSection;
      this.appliedInitialSection = true;
    }
  }

  render() {
    const powerMeter = this.meter ?? this.settings?.power_meter ?? "hass";
    const descriptor = meterFor(powerMeter);
    const connectedGithubUsername = this.contributionAuth?.connected ? this.contributionAuth.identity?.login : "";
    const contributorGithub = this.contributorGithubValue
      ?? (this.settings?.default_contributor_github || connectedGithubUsername || "");
    const defaults = this.settings?.measurement_defaults
      ?? this.capabilities?.defaults
      ?? { sleep_time: 2, sample_count: 1, sleep_time_sample: 1, max_retries: 5, max_nudges: 0 };
    return html`
      <section class="panel" aria-labelledby="settings-title">
        <div class="context">
          <div>
            <p class="eyebrow">Settings</p>
            <h2 id="settings-title">Measurement defaults</h2>
          </div>
        </div>
        <p class="muted">Configure the measurement hardware once and set reusable defaults for new sessions.</p>
        <form ${ref(this.form)} @submit=${this.submit}>
          <div class="settings-layout">
            <nav class="settings-nav" aria-label="Settings sections">
              ${SETTINGS_SECTIONS.map((section) => this.renderNavButton(section))}
            </nav>

            <section class="settings-section" ?hidden=${this.activeSection !== "power_meter"} aria-labelledby="power-meter-title">
              <h3 id="power-meter-title">Power meter</h3>
              <p class="muted">Choose where readings come from and set the hardware metadata added to new profiles.</p>
              <div class="section-fields">
                <measure-combobox
                  name="default_measure_device"
                  label="Power measurement device"
                  .value=${this.measureDeviceValue}
                  .options=${this.measureDeviceOptions()}
                  placeholder="e.g. Shelly Plug S"
                  .hint=${this.measureDevicesLoading
                    ? "Loading names used by existing Powercalc profiles…"
                    : "Manufacturer and model of the meter used to take readings. This is prefilled in each profile and can still be changed there."}
                  required
                  allowCustom
                  @combobox-change=${this.measureDeviceChanged}
                >
                  <input slot="value" type="hidden" name="default_measure_device" .value=${this.measureDeviceValue} />
                </measure-combobox>
                ${this.measureDevicesError
                  ? html`<small class="field-hint error" role="status">Library suggestions are unavailable; manual entry still works.</small>`
                  : nothing}
                <label>
                  <span>Power measurement device firmware</span>
                  <input
                    name="default_measure_device_firmware"
                    .value=${this.settings?.default_measure_device_firmware ?? ""}
                    autocomplete="off"
                    placeholder="Optional firmware version"
                  />
                  <small class="field-hint">Prefilled in new profile metadata and editable for each profile.</small>
                </label>
                ${optionSelect("power_meter", "Type", POWER_METER_LIST.map((meter) => ({ value: meter.type, label: meter.label })), {
                  selected: powerMeter,
                  required: true,
                  placeholder: "Select a power meter type",
                  onChange: this.powerMeterChanged,
                })}
                ${this.renderMeterFields(powerMeter)}
                ${descriptor.qualityNote ? html`<p class="quality-requirements">${descriptor.qualityNote}</p>` : nothing}
                ${descriptor.validatable ? this.renderTestRow() : nothing}
              </div>
              <div class="section-fields witnesses">
                <h4>Witness meters</h4>
                <p class="muted">
                  Optional secondary meters read alongside the primary. Each reading is checked against the
                  primary's within its tolerance before being accepted, which catches a misread or a meter
                  that has drifted or stopped updating.
                </p>
                ${this.witnesses.map((witness, index) => this.renderWitnessRow(witness, index))}
                <button type="button" @click=${this.addWitness}>Add witness</button>
              </div>
            </section>

            <section class="settings-section" ?hidden=${this.activeSection !== "profile"} aria-labelledby="profile-metadata-title">
              <h3 id="profile-metadata-title">Profile metadata</h3>
              <p class="muted">Set contributor details once. They are prefilled when preparing every profile and remain editable there.</p>
              <div class="section-fields">
                <label>
                  <span>Contributor name</span>
                  <input name="default_contributor_name" .value=${this.settings?.default_contributor_name ?? ""} autocomplete="name" />
                </label>
                <label>
                  <span>GitHub username</span>
                  <input
                    name="default_contributor_github"
                    .value=${contributorGithub}
                    @input=${this.contributorGithubChanged}
                    autocomplete="username"
                  />
                </label>
                <label>
                  <span>Email (optional)</span>
                  <input name="default_contributor_email" type="email" .value=${this.settings?.default_contributor_email ?? ""} autocomplete="email" />
                </label>
              </div>
            </section>

            <section class="settings-section" ?hidden=${this.activeSection !== "measure_tuning"} aria-labelledby="measure-tuning-title">
              <h3 id="measure-tuning-title">Measure tuning</h3>
              <p class="muted">Set reusable timing, sampling, and recovery defaults. Relevant values can still be adjusted per measurement.</p>
              ${this.capabilities?.developer_mode ? html`
                <div class="developer-option">
                  <label class="check">
                    <input type="checkbox" name="fast_test_mode" .checked=${this.settings?.fast_test_mode ?? false} />
                    <span>
                      <strong>Fast test mode</strong><br />
                      Synthetic light, fan, and charging workflows only. Skips waits and reduces measurement points so the output is not valid for contribution or real use.
                    </span>
                  </label>
                </div>
              ` : nothing}
              <div class="grid">
                ${this.numberField("sleep_time", "Settle time (seconds)", defaults.sleep_time, 0, 120, "0.1", "Wait after changing a device and between readings.")}
                ${this.numberField("sample_count", "Samples per point", defaults.sample_count, 1, 100, "1", "More samples reduce noise but increase measurement time.")}
                ${this.numberField("sleep_time_sample", "Time between samples (seconds)", defaults.sleep_time_sample, 0, 120, "1", "Used when taking more than one sample per point.")}
                ${this.numberField("max_retries", "Power meter retries", defaults.max_retries, 0, 100, "1", "Consecutive reading errors allowed before aborting.")}
                ${this.numberField("max_nudges", "Stale-reading nudges", defaults.max_nudges, 0, 20, "1", "Temporarily changes a light when its power sensor stops updating. Keep at 0 unless needed.")}
              </div>
            </section>

            <section class="settings-section" ?hidden=${this.activeSection !== "github"} aria-labelledby="github-title">
              <h3 id="github-title">GitHub</h3>
              <p class="muted">Connect GitHub once to open profile-library pull requests from completed measurements.</p>
              ${this.renderGithubSection()}
            </section>
          </div>
          ${this.errorMessage ? html`<p class="notice error" role="alert">${this.errorMessage}</p>` : nothing}
          <div class="actions">
            <button type="button" @click=${() => this.emit("back")}>Back</button>
            <button class="primary" type="submit" ?disabled=${this.busy}>${this.busy ? "Saving…" : "Save settings"}</button>
          </div>
        </form>
      </section>
    `;
  }

  private renderNavButton({ id, label, icon }: SettingsSectionDescriptor) {
    const active = this.activeSection === id;
    return html`
      <button type="button" class=${active ? "active" : ""} aria-current=${active ? "page" : nothing} @click=${() => this.selectSection(id)}>
        <span class="nav-icon" aria-hidden="true">${icon()}</span>
        <span>${label}</span>
      </button>
    `;
  }

  private measureDeviceOptions(): ComboboxOption[] {
    if (this.measureDevicesLoading || this.measureDevicesError) return [];
    return this.measureDevices.map((device) => ({ value: device, label: device }));
  }

  private contributorGithubChanged(event: Event): void {
    this.contributorGithubValue = (event.target as HTMLInputElement).value;
  }

  private measureDeviceChanged(event: CustomEvent<{ value: string }>): void {
    this.measureDeviceValue = event.detail.value;
  }

  private hassPowerEntityChanged(event: CustomEvent<{ value: string }>): void {
    this.hassPowerEntity = event.detail.value;
    this.powerMeterSettingsChanged();
  }

  /**
   * The inputs each meter needs. Kept here rather than in the registry because they are bound to
   * this view's handlers and credential state; the record's type still names any meter left out.
   */
  private renderMeterFields(type: PowerMeterType) {
    const fields: Record<PowerMeterType, () => unknown> = {
      hass: () => this.renderHassFields(),
      shelly: () => this.renderShellyFields(),
      kasa: () => this.renderKasaFields(),
      mystrom: () => this.renderAddressField("mystrom_ip", "myStrom IP address", this.settings?.mystrom_ip),
      tasmota: () => this.renderAddressField("tasmota_ip", "Tasmota IP address", this.settings?.tasmota_ip),
      tuya: () => this.renderTuyaFields(),
      owh98xx: () => this.renderOwonFields(),
      ocr: () => this.renderOcrFields(),
      dummy: () => nothing,
    };
    return fields[type]();
  }

  private renderAddressField(name: string, label: string, value: string | null | undefined) {
    return html`
      <label>
        <span>${label}</span>
        <input name=${name} .value=${value ?? ""} required autocomplete="off" placeholder="192.168.1.50" @input=${this.powerMeterSettingsChanged} />
      </label>`;
  }

  private renderTuyaFields() {
    return html`
      <div class="grid">
        <label>
          <span>Tuya device ID</span>
          <input name="tuya_device_id" .value=${this.settings?.tuya_device_id ?? ""} required autocomplete="off" @input=${this.powerMeterSettingsChanged} />
        </label>
        <label>
          <span>Tuya device IP address</span>
          <input name="tuya_device_ip" .value=${this.settings?.tuya_device_ip ?? ""} required autocomplete="off" placeholder="192.168.1.50" @input=${this.powerMeterSettingsChanged} />
        </label>
      </div>
      <label>
        <span>Tuya protocol version</span>
        <input name="tuya_version" .value=${this.settings?.tuya_version ?? "3.3"} autocomplete="off" @input=${this.powerMeterSettingsChanged} />
        <small class="field-hint">Most devices from 2021 onward use 3.3 or 3.4; check the device's local key discovery output if readings fail.</small>
      </label>`;
  }

  private renderOwonFields() {
    return html`
      <div class="grid">
        <label>
          <span>Serial port</span>
          <input name="owon_port" .value=${this.settings?.owon_port ?? ""} required autocomplete="off" placeholder="/dev/ttyUSB0" @input=${this.powerMeterSettingsChanged} />
        </label>
        <label>
          <span>Baud rate</span>
          <input name="owon_baudrate" type="number" min="1" .value=${String(this.settings?.owon_baudrate ?? 9600)} required @input=${this.powerMeterSettingsChanged} />
        </label>
      </div>
      ${optionSelect("owon_channel", "Channel", [{ value: "1", label: "1" }, { value: "2", label: "2" }], {
        selected: this.settings?.owon_channel ?? "1",
        required: true,
        onChange: this.powerMeterSettingsChanged,
      })}`;
  }

  private renderOcrFields() {
    return html`
      <label>
        <span>Camera source</span>
        <input name="ocr_source" .value=${this.settings?.ocr_source ?? "0"} autocomplete="off" placeholder="http://camera.local:8080/" @input=${this.powerMeterSettingsChanged} />
        <small class="field-hint">An MJPEG stream URL, such as an ESPHome camera's stream endpoint.</small>
      </label>
      <label>
        <span>Display layout</span>
        <input name="ocr_layout" .value=${this.settings?.ocr_layout ?? "pr10"} autocomplete="off" />
        <small class="field-hint">Names a display layout in measure/powermeter/ocr/layout.py describing where each reading appears on screen.</small>
      </label>`;
  }

  private renderHassFields() {
    const options = this.powers.map((entity) => ({
      value: entity.entity_id,
      label: `${entity.name} · ${entity.entity_id}`,
    }));
    return html`
      <measure-combobox
        name="default_power_entity_id"
        label="Power sensor"
        .value=${this.hassPowerEntity}
        .options=${options}
        placeholder="Search power sensors"
        required
        @combobox-change=${this.hassPowerEntityChanged}
      >
        <input slot="value" type="hidden" name="default_power_entity_id" .value=${this.hassPowerEntity} />
      </measure-combobox>`;
  }

  private renderTestRow() {
    return html`
      <div class="test-row">
        <button type="button" @click=${this.test} ?disabled=${this.testing || this.busy}>${this.testing ? "Validating…" : "Validate measurement device"}</button>
        ${this.renderTestResult()}
      </div>`;
  }

  private renderTestResult() {
    if (!this.testResult) return nothing;
    return html`<measure-power-meter-diagnostic .diagnostic=${this.testResult}></measure-power-meter-diagnostic>`;
  }

  private renderGithubIdentity() {
    const identity = this.contributionAuth?.identity;
    const permissionsHint = this.contributionAuth?.permissions_verified === false
      ? html`<span class="field-hint">Identity verified. Fine-grained token permissions can only be confirmed during submission.</span>`
      : nothing;
    return html`
      <div class="identity">
        <div>
          <span class="field-hint">Connected as</span>
          <strong>${identity ? identity.login : "GitHub"}</strong>
          ${permissionsHint}
        </div>
        <button class="danger" type="button" @click=${this.disconnectGithub} ?disabled=${this.contributionAuthBusy}>Disconnect</button>
      </div>
    `;
  }

  private renderGithubConnect() {
    const deviceFlowHint = this.contributionAuth?.device_flow_available === false
      ? html`<p class="field-hint">Device login is not configured for this app build. Use a personal access token.</p>`
      : nothing;
    return html`
      <p class="muted">Connect GitHub to contribute measured profiles. You only need to do this once.</p>
      ${this.renderDeviceFlow()}
      ${deviceFlowHint}
    `;
  }

  private renderGithubSection() {
    return html`
      <div class="section-fields">
        <div class="github-card">
          ${this.contributionAuth?.connected ? this.renderGithubIdentity() : this.renderGithubConnect()}
        </div>
        ${this.contributionAuth?.connected ? nothing : this.renderTokenFallback()}
        <p class="notice">GitHub credentials are stored by the measure app and can be included in Home Assistant backups. Disconnect GitHub before sharing or exporting backups you do not control.</p>
        ${this.contributionAuthError ? html`<p class="notice error" role="alert">${this.contributionAuthError}</p>` : nothing}
      </div>
    `;
  }

  private renderDeviceFlow() {
    if (!this.contributionDeviceFlow) {
      return html`
        <button
          type="button"
          @click=${this.startGithubDeviceLogin}
          ?disabled=${this.contributionAuthBusy || this.contributionAuth?.device_flow_available === false}
        >
          ${this.contributionAuthBusy ? "Starting…" : "Connect GitHub"}
        </button>
      `;
    }
    const status = this.contributionDeviceStatus;
    if (status?.status === "expired" || status?.status === "denied") {
      return html`
        <div class="device-flow">
          <p class="field-hint error" role="alert">${status.message ?? "GitHub authorization did not complete."}</p>
          <button type="button" @click=${this.startGithubDeviceLogin} ?disabled=${this.contributionAuthBusy}>
            ${this.contributionAuthBusy ? "Starting…" : "Get a new code"}
          </button>
        </div>
      `;
    }
    const validMinutes = Math.max(1, Math.ceil(this.contributionDeviceFlow.expires_in / 60));
    return html`
      <div class="device-flow">
        <div class="device-step">
          <p><strong>1. Copy this code</strong></p>
          <div class="device-code-row">
            <input
              class="device-code"
              aria-label="GitHub device code"
              readonly
              .value=${this.contributionDeviceFlow.user_code}
              @focus=${this.selectGithubCode}
            />
            <button type="button" @click=${this.copyGithubCode}>Copy code</button>
          </div>
          ${this.githubCopyStatus ? html`<span class="field-hint" role="status" aria-live="polite">${this.githubCopyStatus}</span>` : nothing}
        </div>
        <div class="device-step">
          <p><strong>2. Authorize Powercalc</strong></p>
          <a class="github-link" href=${this.contributionDeviceFlow.verification_uri} target="_blank" rel="noopener noreferrer">Continue on GitHub ↗</a>
          <span class="field-hint">Paste the code on GitHub. It is valid for up to ${validMinutes} minutes.</span>
        </div>
        <span class="field-hint" role="status" aria-live="polite">
          ${status?.message ?? "Waiting for GitHub authorization… This page will connect automatically when you finish."}
        </span>
      </div>
    `;
  }

  private renderTokenFallback() {
    return html`
      <details class="github-card token-fallback">
        <summary>Use a personal access token instead</summary>
        <label>
          <span>Personal access token</span>
          <div class="token-row">
            <input name="github_token" type="password" autocomplete="off" placeholder="ghp_…" @keydown=${this.tokenKeydown} />
            <button type="button" @click=${this.saveGithubToken} ?disabled=${this.contributionAuthBusy}>Save token</button>
          </div>
          <small class="field-hint">Use only when device login is unavailable.</small>
        </label>
      </details>
    `;
  }

  private renderShellyFields() {
    const address = this.shellyIp ?? this.settings?.shelly_ip ?? "";
    return html`
      <div class="discovery">
        <div class="discovery-header">
          <strong>Discovered Shelly devices</strong>
          <button type="button" @click=${this.discoverShellys} ?disabled=${this.discoveringShellys || this.busy}>
            ${this.discoveringShellys ? "Searching…" : "Refresh"}
          </button>
        </div>
        ${this.renderShellyDiscovery(address)}
      </div>
      <label>
        <span>Shelly IP address</span>
        <input name="shelly_ip" .value=${address} required autocomplete="off" placeholder="192.168.1.50" @input=${this.shellyIpChanged} />
        <small class="field-hint">Select a discovered device above or enter its IP address manually.</small>
      </label>
      <div class="grid">
        <label>
          <span>Shelly username</span>
          <input name="shelly_username" .value=${this.shellyUsername ?? this.settings?.shelly_username ?? DEFAULT_SHELLY_USERNAME} required autocomplete="username" maxlength="50" @input=${this.shellyUsernameChanged} />
          <small class="field-hint">Only used if the device's own "Restrict login" is on. Gen1 devices may use a custom username; Gen2 and newer always use admin regardless of this field.</small>
        </label>
        <label>
          <span>Shelly password</span>
          <input name="shelly_password" type="password" .value=${this.shellyPassword} autocomplete="new-password" maxlength="255" placeholder=${this.settings?.shelly_password_configured ? "Saved password (leave blank to keep)" : "Optional"} @input=${this.shellyPasswordChanged} />
          <small class="field-hint">Leave blank unless the device's own "Restrict login" is on. Stored privately in the app and never returned by the API.</small>
        </label>
      </div>
      ${this.renderClearShellyPassword()}`;
  }

  private renderClearShellyPassword() {
    if (!this.settings?.shelly_password_configured) return nothing;
    return html`
      <label class="check">
        <input name="clear_shelly_password" type="checkbox" .checked=${this.clearShellyPassword} @change=${this.clearShellyPasswordChanged} />
        <span>Remove the saved Shelly password</span>
      </label>`;
  }

  private renderShellyDiscovery(selectedAddress: string) {
    if (this.discoveringShellys) return html`<p class="discovery-status" role="status">Searching for Shelly devices on your network…</p>`;
    if (this.shellyDiscoveryError) return html`<p class="discovery-status error" role="alert">${this.shellyDiscoveryError}</p>`;
    if (this.shellyDiscoveryAvailable === false) {
      return html`<p class="discovery-status">${this.shellyDiscoveryMessage ?? "Shelly discovery is unavailable. Enter the IP address manually."}</p>`;
    }
    if (!this.shellyDiscoveryDevices.length) return html`<p class="discovery-status">No Shelly devices found. You can refresh or enter an IP address manually.</p>`;
    return optionSelect("discovered_shelly", "Select device", [
      { value: "", label: "Select a discovered Shelly" },
      ...this.shellyDiscoveryDevices.map((device) => ({
        value: device.ip_address,
        label: this.shellyDeviceLabel(device),
        disabled: !device.supported && !device.auth_required,
      })),
    ], {
      selected: selectedAddress,
      placeholder: "Search discovered Shelly devices",
      onChange: this.discoveredShellyChanged,
    });
  }

  private renderKasaFields() {
    const address = this.kasaIp ?? this.settings?.kasa_ip ?? "";
    return html`
      <label>
        <span>Kasa IP address</span>
        <input name="kasa_ip" .value=${address} required autocomplete="off" placeholder="192.168.1.50" @input=${this.kasaIpChanged} />
        <small class="field-hint">Enter the IP address of a Kasa plug with energy monitoring, such as a KP115 or HS110. Give it a static lease in your router so it stays reachable.</small>
      </label>`;
  }

  private shellyDeviceLabel(device: ShellyDiscoveryDevice): string {
    const identity = [device.name, device.model, device.generation === null ? null : `Gen ${device.generation}`, device.ip_address]
      .filter((part): part is string => Boolean(part))
      .join(" · ");
    return device.supported ? identity : `${identity} — ${device.reason ?? "Not supported"}`;
  }

  private renderWitnessRow(witness: WitnessSettings, index: number) {
    return html`
      <div class="witness-row">
        <div class="witness-row-header">
          <strong>Witness ${index + 1}</strong>
          <button type="button" class="danger" @click=${() => this.removeWitness(index)}>Remove</button>
        </div>
        ${optionSelect(
          `witness_${index}_type`,
          "Type",
          WITNESS_METER_TYPES.map((type) => ({ value: type, label: meterFor(type).label })),
          {
            selected: witness.meter.type,
            required: true,
            onChange: (event: Event) => this.witnessTypeChanged(index, event),
          },
        )}
        ${this.renderWitnessMeterFields(witness.meter, index)}
        <fieldset class="witness-position">
          <legend>Where this witness sits</legend>
          ${WITNESS_POSITION_OPTIONS.map(
            (option) => html`
              <label class="witness-position-option">
                <input
                  type="radio"
                  name="witness_${index}_position"
                  value=${option.value}
                  .checked=${witness.position === option.value}
                  @change=${(event: Event) => this.witnessPositionChanged(index, event)}
                />
                <span>
                  <strong>${option.label}</strong>
                  <small class="field-hint">${option.hint}</small>
                </span>
              </label>
            `,
          )}
        </fieldset>
        <div class="grid">
          <label>
            <span>Compensation (W)</span>
            <input
              type="number"
              step="0.01"
              min=${witness.position === "none" ? nothing : "0"}
              .value=${String(witness.offset_w)}
              @input=${(event: Event) => this.witnessFieldChanged(index, "offset_w", (event.currentTarget as HTMLInputElement).value)}
            />
            <small class="field-hint">${witness.position === "none"
              ? "How much this witness reads high (positive) or low (negative) versus the truth, if you know it. Leave at 0 if it's not miscalibrated."
              : "Always a magnitude (never negative) — how much power the meter closer to the device draws for itself. See the hint above for whether this also corrects the primary's recorded reading."}</small>
          </label>
          <label>
            <span>Tolerance (W)</span>
            <input type="number" step="0.01" min="0" .value=${String(witness.tolerance_w)} @input=${(event: Event) => this.witnessFieldChanged(index, "tolerance_w", (event.currentTarget as HTMLInputElement).value)} />
          </label>
          <label>
            <span>Tolerance (%)</span>
            <input type="number" step="0.1" min="0" .value=${String(witness.tolerance_pct)} @input=${(event: Event) => this.witnessFieldChanged(index, "tolerance_pct", (event.currentTarget as HTMLInputElement).value)} />
            <small class="field-hint">Whichever of the two tolerances is larger, for the current reading, is the one applied.</small>
          </label>
        </div>
        <label class="check">
          <input type="checkbox" .checked=${witness.required} @change=${(event: Event) => this.witnessRequiredChanged(index, event)} />
          <span>Required — abort the reading (and retry) if this witness disagrees, rather than only warning</span>
        </label>
      </div>`;
  }

  /** The address/entity fields for one witness's meter, using the same layout each meter type
   * uses as a primary but bound to this witness's own draft state instead of `this.settings`. */
  private renderWitnessMeterFields(meter: WitnessMeterSettings, index: number) {
    const set = (field: keyof WitnessMeterSettings) => (event: Event) =>
      this.witnessMeterFieldChanged(index, field, (event.currentTarget as HTMLInputElement).value);
    switch (meter.type) {
      case "hass":
        return html`
          <label>
            <span>Power sensor entity ID</span>
            <input .value=${meter.entity_id ?? ""} required autocomplete="off" placeholder="sensor.plug_power" @input=${set("entity_id")} />
          </label>`;
      case "shelly":
        return html`
          <label>
            <span>Shelly IP address</span>
            <input .value=${meter.device_ip ?? ""} required autocomplete="off" placeholder="192.168.1.51" @input=${set("device_ip")} />
          </label>`;
      case "kasa":
      case "mystrom":
      case "tasmota":
        return html`
          <label>
            <span>IP address</span>
            <input .value=${meter.device_ip ?? ""} required autocomplete="off" placeholder="192.168.1.51" @input=${set("device_ip")} />
          </label>`;
      case "tuya":
        return html`
          <div class="grid">
            <label><span>Tuya device ID</span><input .value=${meter.device_id ?? ""} required autocomplete="off" @input=${set("device_id")} /></label>
            <label><span>Tuya device IP</span><input .value=${meter.device_ip ?? ""} required autocomplete="off" @input=${set("device_ip")} /></label>
          </div>`;
      case "owh98xx":
        return html`
          <div class="grid">
            <label><span>Serial port</span><input .value=${meter.port ?? ""} required autocomplete="off" placeholder="/dev/ttyUSB1" @input=${set("port")} /></label>
            <label><span>Baud rate</span><input type="number" min="1" .value=${String(meter.baudrate ?? 9600)} required @input=${set("baudrate")} /></label>
          </div>`;
      case "ocr":
        return html`
          <label>
            <span>Camera source</span>
            <input .value=${meter.source ?? "0"} autocomplete="off" placeholder="http://camera.local:8080/" @input=${set("source")} />
            <small class="field-hint">An MJPEG stream URL, such as an ESPHome camera's stream endpoint.</small>
          </label>`;
      default:
        return nothing;
    }
  }

  private addWitness(): void {
    this.witnesses = [...this.witnesses, newWitness()];
    this.powerMeterSettingsChanged();
  }

  private removeWitness(index: number): void {
    this.witnesses = this.witnesses.filter((_, i) => i !== index);
    this.powerMeterSettingsChanged();
  }

  private witnessTypeChanged(index: number, event: Event): void {
    const type = (event.currentTarget as HTMLInputElement).value as PowerMeterType;
    this.updateWitness(index, (witness) => ({ ...witness, meter: { type } }));
  }

  private witnessMeterFieldChanged(index: number, field: keyof WitnessMeterSettings, value: string): void {
    const numeric = field === "baudrate" || field === "preview_port" || field === "max_age_seconds";
    this.updateWitness(index, (witness) => ({
      ...witness,
      meter: { ...witness.meter, [field]: numeric ? Number(value) : value },
    }));
  }

  private witnessFieldChanged(index: number, field: "offset_w" | "tolerance_w" | "tolerance_pct", value: string): void {
    this.updateWitness(index, (witness) => ({ ...witness, [field]: Number(value) }));
  }

  /** Changing position can make the current offset sign invalid (only "doesn't affect
   * the primary" allows negative) — clamp it to a magnitude rather than leave a value the
   * backend will reject on submit. */
  private witnessPositionChanged(index: number, event: Event): void {
    const position = (event.currentTarget as HTMLInputElement).value as WitnessPosition;
    this.updateWitness(index, (witness) => ({
      ...witness,
      position,
      offset_w: position === "none" ? witness.offset_w : Math.abs(witness.offset_w),
    }));
  }

  private witnessRequiredChanged(index: number, event: Event): void {
    const required = (event.currentTarget as HTMLInputElement).checked;
    this.updateWitness(index, (witness) => ({ ...witness, required }));
  }

  private updateWitness(index: number, update: (witness: WitnessSettings) => WitnessSettings): void {
    this.witnesses = this.witnesses.map((witness, i) => (i === index ? update(witness) : witness));
    this.powerMeterSettingsChanged();
  }

  private collect(): AppSettingsUpdate | null {
    const element = this.form.value;
    if (!element) return null;
    const data = new FormData(element);
    const meter = settingsFromForm(data);
    // Credentials stay here rather than in the registry: they are only ever entered, never read back.
    const shellyPassword = formRaw(data, "shelly_password");
    return {
      ...meter,
      default_measure_device: formTextOrNull(data, "default_measure_device"),
      default_measure_device_firmware: formTextOrNull(data, "default_measure_device_firmware"),
      default_contributor_name: formTextOrNull(data, "default_contributor_name"),
      default_contributor_github: formTextOrNull(data, "default_contributor_github"),
      default_contributor_email: formTextOrNull(data, "default_contributor_email"),
      shelly_username: formText(data, "shelly_username") || DEFAULT_SHELLY_USERNAME,
      shelly_password_configured: this.settings?.shelly_password_configured ?? false,
      shelly_password: meter.power_meter === "shelly" ? shellyPassword || null : null,
      clear_shelly_password: data.get("clear_shelly_password") === "on",
      witnesses: this.witnesses,
      fast_test_mode: data.get("fast_test_mode") === "on",
      measurement_defaults: {
        sleep_time: this.number(data, "sleep_time"),
        sample_count: this.number(data, "sample_count"),
        sleep_time_sample: this.number(data, "sleep_time_sample"),
        max_retries: this.number(data, "max_retries"),
        max_nudges: this.number(data, "max_nudges"),
      },
    };
  }

  private numberField(name: MeasureParameterName, label: string, value: number, fallbackMin: number, fallbackMax: number, step: string, hint: string) {
    const { min, max } = this.capabilities?.limits?.[name] ?? { min: fallbackMin, max: fallbackMax };
    return html`<label>
      <span>${label}</span>
      <input type="number" name=${name} min=${min} max=${max} step=${step} .value=${String(value)} required />
      <small class="field-hint">${hint}</small>
    </label>`;
  }

  private number(data: FormData, name: string): number {
    return Number(data.get(name));
  }

  private submit(event: SubmitEvent): void {
    event.preventDefault();
    const settings = this.collect();
    if (!settings) return;
    emit<AppSettingsUpdate>(this, "save", settings);
  }

  private test(): void {
    const settings = this.collect();
    if (!settings) return;
    this.testResult = undefined;
    emit<AppSettingsUpdate>(this, "test", settings);
  }

  private powerMeterChanged(event: Event): void {
    this.clearTestResult();
    // Keep the choice in local state so an app-shell re-render can't clobber the
    // in-progress form (which would reset the meter type and typed device IP).
    this.meter = (event.currentTarget as HTMLInputElement).value as PowerMeterType;
    if (meterFor(this.meter).discoverable) this.discoverShellys();
  }

  private powerMeterSettingsChanged(): void {
    this.clearTestResult();
  }

  private shellyIpChanged(event: Event): void {
    this.shellyIp = (event.currentTarget as HTMLInputElement).value;
    this.powerMeterSettingsChanged();
  }

  private shellyUsernameChanged(event: Event): void {
    this.shellyUsername = (event.currentTarget as HTMLInputElement).value;
    this.powerMeterSettingsChanged();
  }

  private shellyPasswordChanged(event: Event): void {
    this.shellyPassword = (event.currentTarget as HTMLInputElement).value;
    this.clearShellyPassword = false;
    this.powerMeterSettingsChanged();
  }

  private clearShellyPasswordChanged(event: Event): void {
    this.clearShellyPassword = (event.currentTarget as HTMLInputElement).checked;
    if (this.clearShellyPassword) this.shellyPassword = "";
    this.powerMeterSettingsChanged();
  }

  private kasaIpChanged(event: Event): void {
    this.kasaIp = (event.currentTarget as HTMLInputElement).value;
    this.powerMeterSettingsChanged();
  }

  private discoveredShellyChanged(event: Event): void {
    const address = (event.currentTarget as HTMLInputElement).value;
    if (!address) return;
    this.shellyIp = address;
    this.powerMeterSettingsChanged();
  }

  private discoverShellys(): void {
    emit(this, "shelly-discover");
  }

  private clearTestResult(): void {
    this.testResult = undefined;
    emit(this, "test-clear");
  }

  private startGithubDeviceLogin(): void {
    this.githubCopyStatus = "";
    emit(this, "github-device-start");
  }

  private selectGithubCode(event: Event): void {
    (event.currentTarget as HTMLInputElement).select();
  }

  private async copyGithubCode(): Promise<void> {
    const code = this.contributionDeviceFlow?.user_code;
    if (!code) return;
    if (await this.writeToClipboard(code)) {
      this.githubCopyStatus = "Code copied.";
      return;
    }
    this.githubCopyStatus = "Couldn’t copy automatically. Select the code and copy it manually.";
    this.shadowRoot?.querySelector<HTMLInputElement>(".device-code")?.select();
  }

  // The async clipboard API only exists in a secure context. Home Assistant is usually reached over
  // plain http on the local network, so inside the ingress panel we have to fall back to execCommand.
  private async writeToClipboard(code: string): Promise<boolean> {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(code);
        return true;
      }
    } catch {
      // Permission or focus problem, try the legacy path below.
    }
    return this.legacyCopy(code);
  }

  private legacyCopy(code: string): boolean {
    const scratch = document.createElement("textarea");
    scratch.value = code;
    scratch.setAttribute("readonly", "");
    scratch.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.append(scratch);
    try {
      scratch.select();
      scratch.setSelectionRange(0, code.length);
      return document.execCommand("copy");
    } catch {
      return false;
    } finally {
      scratch.remove();
    }
  }

  private saveGithubToken(): void {
    const input = this.shadowRoot?.querySelector<HTMLInputElement>('input[name="github_token"]');
    const token = input?.value.trim() ?? "";
    if (!token) return;
    emit<string>(this, "github-token-save", token);
    if (input) input.value = "";
  }

  private tokenKeydown(event: KeyboardEvent): void {
    if (event.key !== "Enter") return;
    event.preventDefault();
    this.saveGithubToken();
  }

  private disconnectGithub(): void {
    emit(this, "github-disconnect");
  }

  private selectSection(section: SettingsSection): void {
    this.activeSection = section;
  }

  private emit(name: "back"): void {
    emit(this, name);
  }
}
