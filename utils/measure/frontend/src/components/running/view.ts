import { LitElement, css, html, nothing, svg, type PropertyValues } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import { createRef, ref } from "lit/directives/ref.js";
import { repeat } from "lit/directives/repeat.js";
import type {
  LogEntry,
  OperatingPoint,
  PlotCollection,
  PlotHoldDetail,
  PlotYViewDetail,
  PowerSample,
  SessionProgress,
  SessionSnapshot,
} from "../../types";
import { emit } from "../../utils/events";
import {
  duration,
  logTime,
  remaining,
  runningActivityLabel,
  sessionModeLabel,
  timestamp,
} from "../../utils/format";
import "./chart";
import { ocrPreviewLabelsFromSpec } from "../../power-meter/registry";
import {
  densityPlotFor,
  plotPairOf,
  reuseYRange,
  sharedYForPlot,
  type PlotPair,
  type PlotYRange,
} from "../result/interaction";
import { diagnosticsDownload, sharedStyles } from "../../styles";
import "../result/plot";
import "../shared/ocr-preview";
import "../shared/sweep-map";
import { hasSweepAxes } from "../shared/sweep-map";

type StateChipIcon =
  | "battery"
  | "brightness"
  | "charging"
  | "color-temp"
  | "effect"
  | "fan-speed"
  | "hue"
  | "muted"
  | "not-charging"
  | "off"
  | "saturation"
  | "volume";

interface StateChip {
  label: string;
  icon: StateChipIcon;
}

/** Keep a short unvirtualized list so tests and the first screen stay exact. */
const LOG_VIRTUALIZE_AFTER = 80;
const LOG_ROW_PX = 28;
const LOG_OVERSCAN = 16;

@customElement("measure-running-view")
export class RunningView extends LitElement {
  @property({ attribute: false })
  snapshot!: SessionSnapshot;

  @property({ type: String })
  confirmationAction = "";

  @property({ type: Boolean })
  warningConfirmation = false;

  @property({ type: Boolean })
  connected = false;

  @property({ type: String })
  lastEventReceivedAt?: string;

  @property({ attribute: false })
  logs: LogEntry[] = [];

  @property({ attribute: false })
  samples: Array<number | PowerSample> = [];

  /**
   * The same shape of profile plot shown on the result screen, refreshed periodically
   * from what's been sampled so far -- builds the shape of the profile as it goes rather
   * than only revealing it once the whole measurement finishes.
   */
  @property({ attribute: false })
  plotCollection: PlotCollection = { partial: true, plots: [], warnings: [] };

  @property({ type: String })
  diagnosticsUrl = "";

  @property({ type: Boolean })
  busy = false;

  /** Labels ("primary", or a witness's name) of whichever OCR camera previews this session has. */
  @property({ attribute: false })
  ocrPreviewLabels: string[] = [];

  @state()
  private nowMs = Date.now();

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

  private elapsedTimer?: number;

  private readonly logContainer = createRef<HTMLDivElement>();

  /** Newest-first: stay glued to the top unless the operator has scrolled away to read. */
  @state()
  private logPinnedToNewest = true;

  @state()
  private logSliceStart = 0;

  private savedLogScrollTop = 0;
  private readonly sharedYByPlot = new Map<string, PlotYRange | null>();
  private newestLogs: LogEntry[] = [];

  private savedLogScrollHeight = 0;

  override connectedCallback(): void {
    super.connectedCallback();
    this.elapsedTimer = window.setInterval(() => {
      this.nowMs = Date.now();
    }, 1000);
  }

  override disconnectedCallback(): void {
    if (this.elapsedTimer !== undefined)
      window.clearInterval(this.elapsedTimer);
    super.disconnectedCallback();
  }

  static readonly styles = [
    sharedStyles,
    css`
      .instrument {
        position: relative;
        overflow: hidden;
        background: var(--well);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: clamp(1.2rem, 4vw, 2rem);
      }
      .topline {
        position: relative;
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 1rem;
      }
      .activity {
        display: flex;
        flex-direction: column;
        gap: 0.28rem;
        min-width: 0;
      }
      .activity-reason {
        color: var(--muted);
        font-size: 0.82rem;
        line-height: 1.4;
        max-width: 42rem;
      }
      .connection {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        color: var(--muted);
        font-size: 0.82rem;
      }
      .connection::before {
        content: "";
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--danger);
      }
      .connection.connected::before {
        background: var(--good);
        box-shadow: 0 0 0 4px color-mix(in srgb, var(--good) 16%, transparent);
      }
      .value {
        position: relative;
        margin: 0.9rem 0 1rem;
        font:
          700 clamp(2.5rem, 7vw, 4rem)/1 "DIN Alternate",
          sans-serif;
        letter-spacing: -0.03em;
        color: var(--signal-strong);
      }
      .value small {
        margin-left: 0.3rem;
        font-size: 0.32em;
        font-weight: 650;
        letter-spacing: 0.04em;
        color: var(--muted);
      }
      progress {
        position: relative;
        display: block;
        width: 100%;
        height: 8px;
        border: 0;
        border-radius: 99px;
        overflow: hidden;
        appearance: none;
      }
      progress::-webkit-progress-bar {
        background: var(--track);
      }
      progress::-webkit-progress-value {
        background: var(--signal);
      }
      progress::-moz-progress-bar {
        background: var(--signal);
      }
      .run-grid {
        position: relative;
        display: flex;
        flex-wrap: wrap;
        gap: 1.1rem 1.25rem;
        align-items: start;
        margin-top: 1.2rem;
      }
      .run-sweeps {
        flex: 1.15 1 14rem;
        min-width: 0;
      }
      .run-metrics {
        flex: 0.55 1 9rem;
        min-width: 0;
      }
      .run-camera {
        flex: 1.4 1 16rem;
        min-width: 0;
      }
      .run-camera .ocr-previews {
        margin: 0;
        grid-template-columns: 1fr;
      }
      .metrics {
        position: relative;
        display: grid;
        gap: 0.75rem;
      }
      .operating-point {
        position: relative;
        margin-top: 1.2rem;
      }
      .operating-point > span {
        display: block;
        margin-bottom: 0.55rem;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.1em;
      }
      .state-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 0.45rem;
      }
      .state-chip {
        display: inline-flex;
        align-items: center;
        gap: 0.38rem;
        padding: 0.38rem 0.65rem;
        border: 1px solid var(--line);
        border-radius: 999px;
        background: color-mix(in srgb, var(--signal) 8%, var(--well));
        font:
          650 0.78rem/1 ui-monospace,
          monospace;
        color: var(--ink);
      }
      .state-icon {
        width: 14px;
        height: 14px;
        flex: none;
        color: var(--signal-strong);
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
        margin-top: 0.25rem;
        font:
          600 1rem/1.3 ui-monospace,
          monospace;
      }
      .topline-right {
        display: inline-flex;
        align-items: center;
        gap: 0.9rem;
      }
      .log-card {
        width: 100%;
        margin: 1.4rem 0 0;
      }
      .log-head {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        margin-bottom: 0.55rem;
      }
      .log-head > span {
        color: var(--muted);
        font:
          700 0.72rem/1 ui-monospace,
          monospace;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }
      .log-head-actions {
        display: inline-flex;
        align-items: center;
        gap: 0.75rem;
      }
      .log-head .log-count {
        color: var(--signal-strong);
        font:
          600 0.78rem/1 ui-monospace,
          monospace;
        letter-spacing: 0;
        text-transform: none;
      }
      .log-jump {
        min-height: 32px;
        padding: 0.3rem 0.7rem;
        font-size: 0.78rem;
      }
      .log {
        max-height: min(36rem, 50vh);
        overflow: auto;
        padding: 0.9rem;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: var(--well);
        font:
          0.8rem/1.6 ui-monospace,
          monospace;
        color: var(--muted);
      }
      .log-spacer {
        pointer-events: none;
      }
      .log p {
        display: flex;
        gap: 0.75rem;
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
      }
      .log p.warning {
        color: var(--warning);
      }
      .log time {
        flex: none;
        color: var(--signal);
        opacity: 0.75;
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
      .ocr-previews {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
        gap: 1rem;
      }
      @media (max-width: 720px) {
        .ocr-previews {
          grid-template-columns: 1fr;
        }
      }
      .chart {
        position: relative;
        margin-top: 1.4rem;
      }
      .chart-head {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 1rem;
      }
      .chart-head span {
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.1em;
      }
      .chart-head strong {
        font:
          700 clamp(1.4rem, 5vw, 2rem)/1 "DIN Alternate",
          sans-serif;
        color: var(--signal-strong);
        letter-spacing: -0.02em;
      }
      .chart-head strong small {
        font-size: 0.5em;
        color: var(--muted);
        letter-spacing: 0.06em;
        margin-left: 0.15em;
      }
      .spark-wrap {
        height: 110px;
        margin-top: 0.6rem;
        overflow: hidden;
        background: repeating-linear-gradient(
          90deg,
          transparent 0,
          transparent calc(10% - 1px),
          color-mix(in srgb, var(--grid) 24%, transparent) 10%
        );
      }
      .spark {
        display: block;
        width: 100%;
        height: 100%;
      }
      .spark .area {
        fill: color-mix(in srgb, var(--signal) 14%, transparent);
        stroke: none;
      }
      .spark .line {
        fill: none;
        stroke: var(--signal);
        stroke-width: 1.6;
        stroke-linejoin: round;
        stroke-linecap: round;
        vector-effect: non-scaling-stroke;
      }
      .chart-scale {
        display: flex;
        justify-content: space-between;
        margin-top: 0.3rem;
        color: var(--muted);
        font:
          0.68rem/1 ui-monospace,
          monospace;
      }
      .entity-states {
        position: relative;
        margin-top: 1.35rem;
        padding-top: 1rem;
        border-top: 1px solid var(--line);
      }
      .entity-states > span {
        display: block;
        margin-bottom: 0.65rem;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.1em;
      }
      .entity-state-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr));
        gap: 0.55rem;
      }
      .entity-state {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 1rem;
        min-width: 0;
        padding: 0.65rem 0.75rem;
        border: 1px solid var(--line);
        border-radius: 9px;
        background: color-mix(in srgb, var(--signal) 5%, var(--well));
      }
      .entity-state code {
        overflow: hidden;
        color: var(--muted);
        font-size: 0.75rem;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .entity-state strong {
        flex: none;
        font:
          650 0.82rem/1.2 ui-monospace,
          monospace;
        color: var(--ink);
      }
      .preparation {
        position: relative;
        display: grid;
        justify-items: center;
        gap: 0.8rem;
        padding: clamp(2rem, 8vw, 4rem) 1rem;
        text-align: center;
      }
      .preparation h3,
      .preparation p {
        margin: 0;
      }
      .preparation-spinner {
        width: 42px;
        height: 42px;
        border: 3px solid var(--track);
        border-top-color: var(--signal);
        border-radius: 50%;
        animation: spin 850ms linear infinite;
      }
      .preparation-track {
        position: relative;
        width: min(360px, 100%);
        height: 8px;
        margin-top: 0.4rem;
        overflow: hidden;
        border-radius: 99px;
        background: var(--track);
      }
      .preparation-bar {
        position: absolute;
        inset-block: 0;
        inset-inline-start: 0;
        width: 38%;
        border-radius: inherit;
        background: var(--signal);
        animation: prepare 1.35s ease-in-out infinite;
      }
      .ready-card {
        display: grid;
        justify-items: center;
        gap: 0.8rem;
        padding: clamp(1.5rem, 6vw, 3rem);
        border: 1px solid color-mix(in srgb, var(--good) 42%, var(--line));
        border-radius: 16px;
        background: color-mix(in srgb, var(--good) 6%, var(--well));
        text-align: center;
      }
      .ready-card.warning {
        border-color: color-mix(in srgb, var(--warning) 58%, var(--line));
        background: color-mix(in srgb, var(--warning) 8%, var(--well));
      }
      .ready-announcement {
        display: grid;
        justify-items: center;
        gap: 0.8rem;
      }
      .ready-announcement h3,
      .ready-announcement p {
        margin: 0;
      }
      .ready-icon {
        display: grid;
        place-items: center;
        width: 46px;
        height: 46px;
        border-radius: 50%;
        background: color-mix(in srgb, var(--good) 16%, transparent);
        color: var(--good);
        font-size: 1.4rem;
      }
      .ready-card.warning .ready-icon {
        width: 52px;
        height: 52px;
        border-radius: 14px;
        background: color-mix(in srgb, var(--warning) 15%, transparent);
        color: var(--warning);
      }
      .ready-card.warning .ready-icon svg {
        width: 34px;
        height: 34px;
        fill: none;
        stroke: currentColor;
        stroke-width: 1.8;
        stroke-linecap: round;
        stroke-linejoin: round;
      }
      .ready-card.warning .ready-eyebrow {
        color: var(--warning);
      }
      .ready-message {
        max-width: 620px;
        color: var(--muted);
        line-height: 1.6;
        white-space: pre-line;
      }
      .ready-topline {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 0.9rem;
        width: 100%;
      }
      @keyframes prepare {
        0% {
          transform: translateX(-105%);
        }
        50% {
          transform: translateX(165%);
        }
        100% {
          transform: translateX(-105%);
        }
      }
      @media (max-width: 640px) {
        .run-sweeps,
        .run-metrics,
        .run-camera {
          flex-basis: 100%;
        }
        .topline {
          align-items: flex-start;
          flex-direction: column;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .preparation-spinner,
        .preparation-bar {
          animation: none;
        }
        .preparation-bar {
          inset-inline-start: 31%;
        }
      }
    `,
  ];

  protected willUpdate(changedProperties: PropertyValues<this>): void {
    if (changedProperties.has("logs")) {
      this.newestLogs = this.logs.slice().reverse();
      if (this.logPinnedToNewest) this.logSliceStart = 0;
    }
    if (!changedProperties.has("logs")) return;
    const container = this.logContainer.value;
    if (!container) return;
    this.savedLogScrollTop = container.scrollTop;
    this.savedLogScrollHeight = container.scrollHeight;
  }

  protected updated(changedProperties: PropertyValues<this>): void {
    if (!changedProperties.has("logs") || !this.logs.length) return;
    const container = this.logContainer.value;
    if (!container) return;
    if (this.logPinnedToNewest) {
      container.scrollTop = 0;
      return;
    }
    // New lines are prepended (newest first). Keep the same line in view.
    const grown = container.scrollHeight - this.savedLogScrollHeight;
    container.scrollTop = this.savedLogScrollTop + Math.max(0, grown);
  }

  private onLogScroll = (event: Event): void => {
    const container = event.currentTarget as HTMLDivElement;
    this.logPinnedToNewest = container.scrollTop <= 8;
    if (this.newestLogs.length <= LOG_VIRTUALIZE_AFTER) return;
    const start = Math.max(0, Math.floor(container.scrollTop / LOG_ROW_PX) - LOG_OVERSCAN);
    if (start !== this.logSliceStart) this.logSliceStart = start;
  };

  private visibleLogs(): { items: LogEntry[]; top: number; bottom: number } {
    const rows = this.newestLogs;
    if (rows.length <= LOG_VIRTUALIZE_AFTER) {
      return { items: rows, top: 0, bottom: 0 };
    }
    const height = this.logContainer.value?.clientHeight ?? 400;
    const visible = Math.ceil(height / LOG_ROW_PX) + LOG_OVERSCAN * 2;
    const start = Math.min(this.logSliceStart, Math.max(0, rows.length - 1));
    const end = Math.min(rows.length, start + visible);
    return {
      items: rows.slice(start, end),
      top: start * LOG_ROW_PX,
      bottom: (rows.length - end) * LOG_ROW_PX,
    };
  }

  private scrollLogToNewest = (): void => {
    this.logPinnedToNewest = true;
    const container = this.logContainer.value;
    if (container) container.scrollTop = 0;
  };

  render() {
    if (this.snapshot.state === "awaiting_confirmation")
      return this.renderReady();
    const preparing = !this.hasMeaningfulProgress();
    const progress = this.snapshot.progress ?? { completed: 0, total: 0 };
    const openEnded =
      this.snapshot.mode === "Recording" && (progress.total ?? 0) === 0;
    return html`
      <section class="panel" aria-labelledby="running-title">
        <p class="eyebrow">03 / Measurement</p>
        <h2 id="running-title">${this.runningTitle(preparing)}</h2>
        <div class="instrument">
          <div class="topline">
            <div class="activity">
              <span class="muted" aria-live="polite"
                >${runningActivityLabel(this.snapshot)}</span
              >
              ${this.snapshot.activity_reason
                ? html`<span class="activity-reason"
                    >${this.snapshot.activity_reason}</span
                  >`
                : nothing}
            </div>
            <span class="topline-right">${this.renderConnection(true)}</span>
          </div>
          ${preparing
            ? this.renderPreparation()
            : this.renderMeasurement(openEnded, progress)}
        </div>
        ${this.renderFooter(openEnded)}
      </section>
    `;
  }

  /** Warnings, the live log card, diagnostics and the stop control — the same on both screens. */
  private renderFooter(openEnded: boolean) {
    return html`
      ${!this.connected ? html`<p class="notice" role="status">
        Reconnecting to live updates. The measurement may still be running in Home Assistant.
        ${this.lastEventReceivedAt ? html`Last update received: <time datetime=${this.lastEventReceivedAt}>${timestamp(this.lastEventReceivedAt)}</time>.` : "Waiting for a live update."}
      </p>` : nothing}
      ${this.renderLatestWarning()} ${this.renderLog()}
      ${diagnosticsDownload(this.diagnosticsUrl)}
      <div class="actions">${this.renderStopButton(openEnded)}</div>
    `;
  }

  private renderConnection(announce: boolean) {
    return html`<span
      class="connection ${this.connected ? "connected" : ""}"
      role=${announce ? "status" : nothing}
    >
      ${this.connected ? "Live" : "Reconnecting"}
    </span>`;
  }

  private renderReady() {
    const message =
      this.snapshot.confirmation_message ??
      "Preparation is complete. Start the measurement when the device is ready.";
    const warning = this.warningConfirmation;
    return html`
      <section class="panel" aria-labelledby="running-title">
        <p class="eyebrow">03 / Measurement</p>
        <h2 id="running-title">Ready when you are</h2>
        <div class="ready-card ${warning ? "warning" : ""}">
          <span class="ready-topline">${this.renderConnection(false)}</span>
          <div
            class="ready-announcement"
            role=${warning ? "alert" : "status"}
            aria-live=${warning ? "assertive" : "polite"}
          >
            <span class="ready-icon" aria-hidden="true"
              >${warning
                ? svg`
              <svg viewBox="0 0 24 24">
                <path d="M10.3 3.7 2.2 18a2 2 0 0 0 1.7 3h16.2a2 2 0 0 0 1.7-3L13.7 3.7a2 2 0 0 0-3.4 0Z"></path>
                <path d="M12 9v4"></path><path d="M12 17h.01"></path>
              </svg>
            `
                : "✓"}</span
            >
            <p class="eyebrow ready-eyebrow">
              ${warning ? "High volume warning" : "Preparation complete"}
            </p>
            <h3>${warning ? "Protect your hearing" : "Everything is ready"}</h3>
            <p class="ready-message">${message}</p>
          </div>
          <button class="primary confirm" type="button" @click=${this.confirm} ?disabled=${this.busy}>${this.busy ? "Starting…" : this.confirmationAction || "Start measurement"}</button>
        </div>
        ${this.renderFooter(false)}
      </section>
    `;
  }

  private renderMeasurement(openEnded: boolean, progress: SessionProgress) {
    return html`
      ${this.renderProgress(openEnded, progress)}
      ${this.renderCalibrationSample()}
      ${this.snapshot.operating_point
        ? this.renderOperatingPoint(this.snapshot.operating_point)
        : nothing}
      ${this.renderRunLayout(openEnded, progress)}
      ${this.samples.length ? html`<measure-power-chart .samples=${this.sampleWatts()}></measure-power-chart>` : nothing}
      ${this.renderProfilePlots()} ${this.renderEntityStates()}
    `;
  }

  private renderOcrPreviews() {
    if (!this.snapshot.session_id) return nothing;
    const labels =
      this.ocrPreviewLabels.length > 0
        ? this.ocrPreviewLabels
        : ocrPreviewLabelsFromSpec(this.snapshot.request?.power_meter);
    if (!labels.length) return nothing;
    return html`
      <div class="ocr-previews">
        ${labels.map(
          (label) => html`
            <measure-ocr-preview
              beside
              .sessionId=${this.snapshot.session_id!}
              .label=${label}
            ></measure-ocr-preview>
          `,
        )}
      </div>
    `;
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

  private stableSharedY(plotId: string): PlotYRange | null {
    const next = reuseYRange(
      this.sharedYByPlot.get(plotId),
      sharedYForPlot(this.plotCollection.plots, plotId),
    );
    this.sharedYByPlot.set(plotId, next);
    return next;
  }

  private clearPlotHold(): void {
    this.heldPointId = null;
    this.heldSourcePlotId = null;
    this.heldPair = null;
    this.linkedHue = null;
    this.linkedBri = null;
    this.linkedMired = null;
  };

  private renderProfilePlots() {
    const { plots } = this.plotCollection;
    if (!plots.length) return nothing;
    return html`
      <div class="plots-header"><h3>Profile so far</h3></div>
      <div class="plots">
        ${plots.map(
          (plot) =>
            html`<measure-result-plot
              .plot=${plot}
              partial
              .fadeInherited=${true}
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
              .filterMired=${plot.id === "color_temp" ? this.linkedMired : null}
              .densityPlot=${densityPlotFor(plots, plot)}
              @plot-hue-link=${this.onPlotHueLink}
              @plot-bri-link=${this.onPlotBriLink}
              @plot-mired-link=${this.onPlotMiredLink}
              @plot-hold=${this.onPlotHold}
              @plot-y-view=${this.onPlotYView}
            ></measure-result-plot>`,
        )}
      </div>
    `;
  }

  private renderPreparation() {
    const phase = this.snapshot.phase ?? "Preparing measurement devices";
    const remainingSeconds = this.liveWaitRemainingSeconds();
    const waitSeconds = this.snapshot.wait_seconds;
    const knownWait =
      remainingSeconds != null && waitSeconds != null && waitSeconds > 0;
    const elapsedFraction = knownWait
      ? Math.min(1, Math.max(0, (waitSeconds - remainingSeconds) / waitSeconds))
      : 0;
    return html`
      <div class="preparation" role="status" aria-live="polite">
        ${knownWait
          ? nothing
          : html`<span class="preparation-spinner" aria-hidden="true"></span>`}
        <h3>${phase}</h3>
        <p class="muted">
          ${remainingSeconds != null
            ? `${duration(remainingSeconds)} left`
            : "Connecting the light and power meter."}
        </p>
        ${knownWait
          ? html`<progress
              max="100"
              .value=${elapsedFraction * 100}
              aria-label="${duration(remainingSeconds)} left"
            ></progress>`
          : html`<span class="preparation-track" aria-hidden="true"
              ><span class="preparation-bar"></span
            ></span>`}
      </div>
      ${this.snapshot.operating_point
        ? this.renderOperatingPoint(this.snapshot.operating_point)
        : nothing}
      ${this.samples.length ? html`<measure-power-chart .samples=${this.sampleWatts()}></measure-power-chart>` : nothing}
      ${this.renderEntityStates()}
    `;
  }

  private renderEntityStates() {
    const states = Object.entries(this.snapshot.entity_states ?? {});
    if (!states.length) return nothing;
    return html`
      <div class="entity-states" aria-live="polite">
        <span>Tracked entities</span>
        <div class="entity-state-grid">
          ${states.map(
            ([entityId, state]) => html`
              <div class="entity-state">
                <code title=${entityId}>${entityId}</code
                ><strong>${state}</strong>
              </div>
            `,
          )}
        </div>
      </div>
    `;
  }

  private hasMeaningfulProgress(): boolean {
    const progress = this.snapshot.progress;
    if (!progress) return false;
    if (this.snapshot.mode === "Recording") return true;
    return progress.completed > 0;
  }

  private liveWaitRemainingSeconds(): number | null {
    const ends = this.snapshot.wait_ends_at;
    if (!ends || !this.isLiveTiming()) return null;
    const parsed = Date.parse(ends);
    if (Number.isNaN(parsed)) return null;
    return Math.max(0, (parsed - this.nowMs) / 1000);
  }

  private renderProgress(openEnded: boolean, progress: SessionProgress) {
    if (openEnded) {
      return html`<div
          class="value"
          aria-label="${progress.completed} samples recorded"
        >
          ${progress.completed}<small>samples</small>
        </div>
        <progress max="100" aria-label="Recording"></progress>`;
    }
    const percent =
      progress.percent ??
      (progress.total ? (progress.completed / progress.total) * 100 : 0);
    const percentLabel =
      percent > 0 && Math.round(percent) === 0
        ? "<1"
        : String(Math.round(percent));
    return html`<div
        class="value"
        aria-label="${percentLabel} percent complete"
      >
        ${percentLabel}<small>%</small>
      </div>
      <progress max="100" .value=${percent}>${percentLabel}%</progress>`;
  }

  private isLiveTiming(): boolean {
    return (
      this.snapshot.state === "running" ||
      this.snapshot.state === "cancelling" ||
      this.snapshot.state === "awaiting_confirmation"
    );
  }

  private liveElapsedSeconds(progress: SessionProgress): number | null {
    const started = this.snapshot.run_started_at;
    if (this.isLiveTiming() && started) {
      const parsed = Date.parse(started);
      if (!Number.isNaN(parsed))
        return Math.max(0, (this.nowMs - parsed) / 1000);
    }
    return progress.elapsed_seconds ?? null;
  }

  private sessionRecorded(progress: SessionProgress): number | null {
    const already = progress.already_measured ?? 0;
    if (already <= 0) return null;
    return Math.max(0, progress.completed - already);
  }

  private liveRemainingSeconds(progress: SessionProgress): number | null {
    const elapsed = this.liveElapsedSeconds(progress);
    const completed = progress.completed;
    const total = progress.total;
    const already = progress.already_measured ?? 0;
    const measuredHere = completed - already;
    const needed = already > 0 ? 5 : 1;
    if (elapsed != null && measuredHere >= needed && total > completed) {
      return elapsed * ((total - completed) / measuredHere);
    }
    if (total > 0 && completed >= total) return 0;
    return progress.estimated_remaining_seconds ?? null;
  }

  private renderMetrics(openEnded: boolean, progress: SessionProgress) {
    let progressLabel = "Variation";
    if (openEnded) progressLabel = "Recorded";
    else if (
      this.snapshot.mode === "Averaging" ||
      this.snapshot.mode === "Trickle charging"
    )
      progressLabel = "Seconds";
    else if (this.snapshot.mode === "Charging") progressLabel = "Battery";
    const recordedHere = this.sessionRecorded(progress);
    return html`
      <div class="metrics">
        <div class="metric">
          <span>Mode</span
          ><strong>${sessionModeLabel(this.snapshot.mode)}</strong>
        </div>
        ${this.snapshot.phase === "Discovering envelope" ||
        this.snapshot.phase === "Covering interior" ||
        this.snapshot.phase === "Throwing darts"
          ? html`<div class="metric">
              <span>Phase</span><strong>${this.snapshot.phase}</strong>
            </div>`
          : nothing}
        <div class="metric">
          <span>${progressLabel}</span
          ><strong
            >${openEnded
              ? progress.completed
              : html`${progress.completed} / ${progress.total}`}</strong
          >
        </div>
        ${recordedHere != null
          ? html`<div class="metric">
              <span>This session</span><strong>${recordedHere}</strong>
            </div>`
          : nothing}
        ${progress.skipped
          ? html`<div class="metric">
              <span>Skipped</span><strong>${progress.skipped}</strong>
            </div>`
          : nothing}
        <div class="metric">
          <span>Time elapsed</span
          ><strong
            >${this.liveElapsedSeconds(progress) == null
              ? "—"
              : duration(this.liveElapsedSeconds(progress)!)}</strong
          >
        </div>
        ${this.liveWaitRemainingSeconds() != null
          ? html`<div class="metric">
              <span>This wait</span
              ><strong>${duration(this.liveWaitRemainingSeconds()!)}</strong>
            </div>`
          : nothing}
        <div class="metric">
          <span>Time remaining</span
          ><strong
            >${openEnded
              ? "Until stopped"
              : remaining(this.liveRemainingSeconds(progress))}</strong
          >
        </div>
      </div>
    `;
  }

  private renderCalibrationSample() {
    const sample = this.snapshot.calibration_sample;
    const phase = `${this.snapshot.phase ?? ""} ${this.snapshot.mode ?? ""}`;
    if (!sample || !/dummy[- ]load/i.test(phase)) return nothing;
    return html`
      <div
        class="metrics calibration-metrics"
        aria-label="Live dummy-load calibration reading"
      >
        <div class="metric">
          <span>Wattage</span><strong>${sample.power.toFixed(2)} W</strong>
        </div>
        <div class="metric">
          <span>Resistance</span
          ><strong>${sample.resistance.toFixed(2)} Ω</strong>
        </div>
        <div class="metric">
          <span>Voltage</span><strong>${sample.voltage.toFixed(2)} V</strong>
        </div>
      </div>
    `;
  }

  private renderStopButton(openEnded: boolean) {
    const cancelling = this.snapshot.state === "cancelling";
    const averaging =
      this.snapshot.mode === "Averaging" &&
      this.snapshot.state !== "awaiting_confirmation";
    if (openEnded || averaging) {
      let label = "Stop measurement";
      if (cancelling) label = "Stopping…";
      else if (openEnded) label = "Stop recording";
      return html`<button class="primary" type="button" @click=${this.cancel} ?disabled=${this.busy || cancelling}>${label}</button>`;
    }
    return html`<button class="danger" type="button" @click=${this.cancel} ?disabled=${this.busy || cancelling}>${cancelling ? "Cancelling…" : "Cancel measurement"}</button>`;
  }

  private sampleWatts(): number[] {
    return this.powerSamples().map((sample) => sample.power);
  }

  private powerSamples(): PowerSample[] {
    return this.samples.map((sample, index) =>
      typeof sample === "number"
        ? { power: sample, at: String(index) }
        : sample,
    );
  }

  private renderRunLayout(openEnded: boolean, progress: SessionProgress) {
    const sweeps = hasSweepAxes(this.snapshot.sweep_coverage)
      ? html`<div class="run-sweeps">
          <measure-sweep-map
            .coverage=${this.snapshot.sweep_coverage}
            live
          ></measure-sweep-map>
        </div>`
      : nothing;
    const camera = this.renderOcrPreviews();
    return html`
      <div class="run-grid">
        ${sweeps}
        <div class="run-metrics">
          ${this.renderMetrics(openEnded, progress)}
        </div>
        ${camera ? html`<div class="run-camera">${camera}</div>` : nothing}
      </div>
    `;
  }

  private renderOperatingPoint(point: OperatingPoint) {
    const chips = this.operatingPointChips(point);
    return html`
      <div class="operating-point" aria-live="polite">
        <span>Current measurement point</span>
        <div class="state-chips">
          ${chips.map(
            (chip) =>
              html`<span class="state-chip"
                >${this.stateIcon(chip.icon)}${chip.label}</span
              >`,
          )}
        </div>
      </div>
    `;
  }

  private operatingPointChips(point: OperatingPoint): StateChip[] {
    switch (point.type) {
      case "light":
        return this.lightChips(point);
      case "speaker":
        return [
          {
            label: point.muted ? "Muted" : `Volume ${point.volume}%`,
            icon: point.muted ? "muted" : "volume",
          },
        ];
      case "fan":
        return [
          {
            label: point.on ? `Fan speed ${point.percentage}%` : "Off",
            icon: point.on ? "fan-speed" : "off",
          },
        ];
      case "charging":
        return [
          { label: `Battery ${point.battery_level}%`, icon: "battery" },
          {
            label: point.charging ? "Charging" : "Not charging",
            icon: point.charging ? "charging" : "not-charging",
          },
        ];
    }
  }

  private lightChips(
    point: Extract<OperatingPoint, { type: "light" }>,
  ): StateChip[] {
    if (!point.on) return [{ label: "Off", icon: "off" }];
    const chips: StateChip[] = [];
    if (typeof point.brightness === "number")
      chips.push({
        label: `Brightness ${Math.round((point.brightness / 255) * 100)}%`,
        icon: "brightness",
      });
    if (typeof point.color_temp_mired === "number")
      chips.push({
        label: `Color temp ${Math.round(1_000_000 / point.color_temp_mired)} K`,
        icon: "color-temp",
      });
    if (typeof point.hue === "number")
      chips.push({
        label: `Hue ${Math.round((point.hue / 65_535) * 360)}°`,
        icon: "hue",
      });
    if (typeof point.saturation === "number")
      chips.push({
        label: `Saturation ${Math.round((point.saturation / 255) * 100)}%`,
        icon: "saturation",
      });
    if (point.effect)
      chips.push({ label: `Effect ${point.effect}`, icon: "effect" });
    return chips;
  }

  private stateIcon(icon: StateChipIcon) {
    const content = {
      battery: svg`<rect x="2" y="4.25" width="11" height="7.5" rx="1.25"></rect><path d="M14 6.5v3"></path><path d="M4 6.5h5"></path>`,
      brightness: svg`<circle cx="8" cy="8" r="2.25"></circle><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.4 1.4M11.55 11.55l1.4 1.4M12.95 3.05l-1.4 1.4M4.45 11.55l-1.4 1.4"></path>`,
      charging: svg`<path d="m9.2 1.8-5 7h3.3l-.7 5.4 5-7H8.5l.7-5.4Z"></path>`,
      "color-temp": svg`<path d="M6.2 9.5V3.3a1.8 1.8 0 0 1 3.6 0v6.2a3 3 0 1 1-3.6 0Z"></path><path d="M8 5v6"></path>`,
      effect: svg`<path d="m8 1 .8 3.2L12 5l-3.2.8L8 9l-.8-3.2L4 5l3.2-.8L8 1Z"></path><path d="m12.5 9 .45 1.55 1.55.45-1.55.45L12.5 13l-.45-1.55-1.55-.45 1.55-.45L12.5 9Z"></path>`,
      "fan-speed": svg`<circle cx="8" cy="8" r="1.15" fill="currentColor" stroke="none"></circle><path d="M8.5 6.9c.3-2.6 1.35-4.2 2.7-3.7 1.5.55 1.25 2.9-.15 4.25"></path><path d="M8.7 8.95c2.4 1.05 3.25 2.8 2.1 3.65-1.3.95-3.2-.45-3.7-2.3"></path><path d="M6.8 8.15c-2.1 1.55-4.05 1.4-4.25-.05-.2-1.6 1.95-2.55 3.8-2.05"></path>`,
      hue: svg`<circle cx="8" cy="8" r="5.5"></circle><path d="M8 2.5v3M13.5 8h-3M8 13.5v-3M2.5 8h3"></path>`,
      muted: svg`<path d="M2 6h2.5L8 3v10l-3.5-3H2V6Z"></path><path d="m11 6 3 4m0-4-3 4"></path>`,
      "not-charging": svg`<path d="m9.2 1.8-3 4.2M5 8H4.2l.55-.78M7.5 8h4.3l-2.25 3.15M8.8 12.2l-2 2 .28-2.2"></path><path d="m2 2 12 12"></path>`,
      off: svg`<path d="M8 1.5v6"></path><path d="M4.2 3.7a5.5 5.5 0 1 0 7.6 0"></path>`,
      saturation: svg`<path d="M8 1.5s4.5 5 4.5 8.2a4.5 4.5 0 1 1-9 0C3.5 6.5 8 1.5 8 1.5Z"></path><path d="M5.7 10.2c.25 1.1 1.05 1.65 2 1.8"></path>`,
      volume: svg`<path d="M2 6h2.5L8 3v10l-3.5-3H2V6Z"></path><path d="M10.5 5.5a3.5 3.5 0 0 1 0 5M12.5 3.5a6.2 6.2 0 0 1 0 9"></path>`,
    } satisfies Record<StateChipIcon, unknown>;
    return html`
      <svg
        class="state-icon"
        data-state-icon=${icon}
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        stroke-width="1.35"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        ${content[icon]}
      </svg>
    `;
  }

  private renderLog() {
    return html`
      <section class="log-card" aria-label="Measurement log">
        <div class="log-head">
          <span>Measurement log</span>
          <div class="log-head-actions">
            ${this.logPinnedToNewest
              ? nothing
              : html`<button
                  type="button"
                  class="log-jump"
                  @click=${this.scrollLogToNewest}
                >
                  Scroll to top
                </button>`}
            <span class="log-count">${this.logs.length} lines</span>
          </div>
        </div>
        <div
          ${ref(this.logContainer)}
          class="log"
          aria-live="polite"
          @scroll=${this.onLogScroll}
        >
          ${this.newestLogs.length
            ? this.renderLogLines()
            : html`<p>Waiting for log lines…</p>`}
        </div>
      </section>
    `;
  }

  private renderLogLines() {
    const slice = this.visibleLogs();
    return html`
      <div class="log-spacer" style="height:${slice.top}px"></div>
      ${repeat(
        slice.items,
        (log) => log.sequence ?? `${log.time}\0${log.message}`,
        (log) => this.logLine(log),
      )}
      <div class="log-spacer" style="height:${slice.bottom}px"></div>
    `;
  }

  private logLine(log: LogEntry) {
    const warning = this.snapshot.warnings?.includes(log.message) ?? false;
    return html`<p class=${warning ? "warning" : ""}>
      <time datetime=${log.time}>${logTime(log.time)}</time
      ><span>${log.message}</span>
    </p>`;
  }

  private renderLatestWarning() {
    const warning = this.snapshot.warnings?.at(-1);
    return warning
      ? html`<div class="notice warning" role="alert">${warning}</div>`
      : nothing;
  }

  private cancel(): void {
    emit(this, "cancel");
  }

  private confirm(): void {
    emit(this, "confirm");
  }

  private runningTitle(preparing = false): string {
    if (this.snapshot.state === "cancelling") return "Stopping safely";
    if (this.snapshot.state === "awaiting_confirmation")
      return "Ready when you are";
    if (this.snapshot.phase === "Discovering envelope")
      return "Discovering envelope";
    if (this.snapshot.phase === "Covering interior") return "Covering interior";
    if (this.snapshot.phase === "Throwing darts") return "Throwing darts";
    if (preparing) return "Preparing measurement";
    return "Sampling in progress";
  }
}
