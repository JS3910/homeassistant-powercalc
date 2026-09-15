import { LitElement, css, html, nothing, type PropertyValues } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import { apiUrl } from "../../api-client";
import { emit } from "../../utils/events";
import type { OcrPreviewState } from "../../types";
import { sharedStyles } from "../../styles";

const HEARTBEAT_MS = 15_000;
const RETRY_MS = 2_000;

/**
 * Live camera feed + parsed reading for one OCR meter in a running session, labelled
 * "primary" or by witness name (see `measure/ha_app/ocr_preview.py`). Reaches the same
 * `PreviewServer` the standalone preview page would (`measure/powermeter/ocr/preview.py`),
 * just through this app's own ingress-safe API instead of a second socket -- an `EventSource`
 * per published frame, driving both the annotated image and the numbers below it.
 */
@customElement("measure-ocr-preview")
export class OcrPreview extends LitElement {
  @property({ type: String })
  sessionId = "";

  @property({ type: String })
  label = "";

  /**
   * `"session"` reaches a running session's meter (`api/sessions/{id}/ocr/...`);
   * `"preview"` reaches a throwaway camera-aiming preview started before any session
   * exists, keyed by a preview id instead of a session id
   * (`api/power-meters/ocr-preview/{id}/...`, see `measure/ha_app/meter_preview.py`).
   * Both expose the identical state/frame.jpg/events shape, so this component doesn't
   * otherwise need to know which one it's looking at.
   */
  @property({ type: String })
  kind: "session" | "preview" = "session";

  /** Put the telemetry table beside the frame instead of under it. */
  @property({ type: Boolean, reflect: true })
  beside = false;

  @state()
  private status: "connecting" | "live" | "disconnected" = "connecting";

  @state()
  private frameVersion = -1;

  @state()
  private frameSrc = "";

  @state()
  private frameBroken = false;

  @state()
  private previewState?: OcrPreviewState;

  private eventSource?: EventSource;
  private retryTimer?: number;
  private heartbeatTimer?: number;
  private frameObjectUrl = "";
  private frameRequest = 0;
  private reconnectingPreview = false;
  private allowReconnect = false;
  private connectedKey = "";

  static readonly styles = [
    sharedStyles,
    css`
      :host {
        display: block;
        min-width: 0;
      }
      .card {
        padding: 1rem;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: var(--field);
      }
      .head {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 1rem;
        margin-bottom: 0.75rem;
      }
      h4 {
        margin: 0;
        font-size: 1rem;
        text-transform: capitalize;
      }
      .connection {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        color: var(--muted);
        font-size: 0.72rem;
      }
      .connection.live {
        color: var(--good);
      }
      .connection.disconnected {
        color: var(--danger);
      }
      .reconnect {
        min-height: 32px;
        padding: 0.25rem 0.65rem;
        font-size: 0.72rem;
      }
      .body {
        display: grid;
        gap: 0.75rem;
      }
      :host([beside]) .body {
        grid-template-columns: minmax(0, 1.15fr) minmax(13rem, 0.85fr);
        grid-template-rows: 16.5rem;
        align-items: stretch;
      }
      .frame {
        position: relative;
        background: #000;
        border-radius: 8px;
        overflow: hidden;
        width: 100%;
        height: 16.5rem;
      }
      :host([beside]) table {
        min-width: 0;
        margin-top: 0;
        overflow: auto;
      }
      :host([beside]) td.value {
        white-space: nowrap;
      }
      @media (max-width: 640px) {
        :host([beside]) .body {
          grid-template-columns: 1fr;
          grid-template-rows: auto;
        }
      }
      img {
        display: block;
        width: 100%;
        height: 100%;
        object-fit: contain;
      }
      .placeholder {
        display: grid;
        place-items: center;
        height: 100%;
        color: var(--muted);
        font-size: 0.8rem;
      }
      table {
        width: 100%;
        margin-top: 0.75rem;
        border-collapse: collapse;
        font:
          0.78rem/1.4 ui-monospace,
          monospace;
      }
      td {
        padding: 0.2rem 0;
        border-bottom: 1px solid var(--line);
      }
      td:first-child {
        color: var(--muted);
      }
      td.value {
        text-align: right;
        white-space: nowrap;
      }
      td.note {
        text-align: left;
        white-space: normal;
        overflow-wrap: anywhere;
        word-break: break-word;
      }
      tr.reason td {
        padding-top: 0;
        border-bottom: 1px solid var(--line);
        font-size: 0.72rem;
        line-height: 1.35;
      }
      .ok {
        color: var(--good);
      }
      .bad {
        color: var(--danger);
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    this.connect();
    this.startHeartbeat();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.teardown();
  }

  protected updated(_changed: PropertyValues<this>): void {
    if (this.identityKey() !== this.connectedKey) this.connect();
  }

  private identityKey(): string {
    return this.sessionId && this.label
      ? `${this.kind}:${this.sessionId}:${this.label}`
      : "";
  }

  private basePath(): string {
    return this.kind === "preview"
      ? `api/power-meters/ocr-preview/${encodeURIComponent(this.sessionId)}`
      : `api/sessions/${encodeURIComponent(this.sessionId)}/ocr`;
  }

  private connect(): void {
    this.clearRetry();
    this.eventSource?.close();
    this.eventSource = undefined;
    this.connectedKey = this.identityKey();
    if (!this.sessionId || !this.label) return;
    this.status = "connecting";
    const url = apiUrl(
      `${this.basePath()}/${encodeURIComponent(this.label)}/events`,
    );
    const source = new EventSource(url.toString());
    this.eventSource = source;
    source.onopen = () => {
      this.status = "live";
      this.reconnectingPreview = false;
    };
    source.onerror = () => {
      this.allowReconnect = true;
      this.status = "disconnected";
      source.close();
      if (this.eventSource === source) this.eventSource = undefined;
      this.scheduleRetry();
    };
    source.onmessage = (event: MessageEvent<string>) => {
      const message = JSON.parse(event.data) as {
        version: number;
        state: OcrPreviewState;
      };
      this.status = "live";
      this.reconnectingPreview = false;
      this.previewState = message.state;
      if (message.version !== this.frameVersion) {
        this.frameVersion = message.version;
        this.frameBroken = false;
        void this.loadFrame(message.version);
      }
    };
  }

  private scheduleRetry(): void {
    if (this.retryTimer !== undefined || !this.sessionId) return;
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = undefined;
      this.connect();
    }, RETRY_MS);
  }

  private clearRetry(): void {
    if (this.retryTimer === undefined) return;
    window.clearTimeout(this.retryTimer);
    this.retryTimer = undefined;
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = window.setInterval(
      () => void this.heartbeat(),
      HEARTBEAT_MS,
    );
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer === undefined) return;
    window.clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = undefined;
  }

  private teardown(): void {
    this.clearRetry();
    this.stopHeartbeat();
    this.eventSource?.close();
    this.eventSource = undefined;
    this.forgetFrame();
  }

  private forgetFrame(): void {
    this.frameRequest += 1;
    if (this.frameObjectUrl) {
      URL.revokeObjectURL(this.frameObjectUrl);
      this.frameObjectUrl = "";
    }
    this.frameSrc = "";
  }

  private async loadFrame(version: number): Promise<void> {
    if (!this.sessionId || !this.label || version < 0) return;
    const request = ++this.frameRequest;
    try {
      const response = await fetch(
        apiUrl(
          `${this.basePath()}/${encodeURIComponent(this.label)}/frame.jpg?v=${version}`,
        ).toString(),
        { credentials: "same-origin", cache: "no-store" },
      );
      if (!response.ok || request !== this.frameRequest) return;
      const blob = await response.blob();
      if (request !== this.frameRequest) return;
      const next = URL.createObjectURL(blob);
      const previous = this.frameObjectUrl;
      this.frameObjectUrl = next;
      this.frameSrc = next;
      if (previous) URL.revokeObjectURL(previous);
    } catch {
      if (request === this.frameRequest) this.frameBroken = true;
    }
  }

  /**
   * Poll `/state` so a dropped EventSource still touches the idle reaper, and so a
   * reaped aiming preview can be rebuilt without resetting the setup form.
   */
  private async heartbeat(): Promise<void> {
    if (!this.sessionId || !this.label) return;
    try {
      const response = await fetch(
        apiUrl(`${this.basePath()}/${encodeURIComponent(this.label)}/state`),
        {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        },
      );
      if (response.status === 404) {
        this.allowReconnect = true;
        this.status = "disconnected";
        this.requestPreviewRestart();
        return;
      }
      if (!response.ok) return;
      this.previewState = (await response.json()) as OcrPreviewState;
    } catch {
      this.allowReconnect = true;
      this.status = "disconnected";
    }
  }

  private requestPreviewRestart(): void {
    if (this.kind !== "preview" || this.reconnectingPreview) return;
    this.reconnectingPreview = true;
    emit(this, "ocr-preview-reconnect");
  }

  private reconnect = (): void => {
    this.requestPreviewRestart();
    this.connect();
  };

  private onFrameError = (): void => {
    this.frameBroken = true;
    this.allowReconnect = true;
    if (this.status === "live") this.status = "disconnected";
  };

  render() {
    const state = this.previewState;
    return html`
      <div class="card">
        <div class="head">
          <h4>${this.label}</h4>
          <span class="connection ${this.status}">
            ${this.status === "live"
              ? "Live"
              : this.status === "disconnected"
                ? "Disconnected"
                : "Connecting…"}
            ${this.allowReconnect && this.status !== "live"
              ? html`<button
                  type="button"
                  class="reconnect"
                  @click=${this.reconnect}
                >
                  Reconnect
                </button>`
              : nothing}
          </span>
        </div>
        <div class="body">
          <div class="frame">
            ${this.frameSrc && !this.frameBroken
              ? html`<img
                  src=${this.frameSrc}
                  alt="${this.label} camera preview"
                  @error=${this.onFrameError}
                />`
              : html`<div class="placeholder">
                  ${this.status === "disconnected"
                    ? "Camera disconnected"
                    : "Waiting for a frame…"}
                </div>`}
          </div>
          ${state ? this.renderTable(state) : nothing}
        </div>
      </div>
    `;
  }

  private renderTable(state: OcrPreviewState) {
    const last = state.last;
    const number = (
      value: number | null | undefined,
      digits: number,
      unit: string,
    ) => (value == null ? "—" : `${value.toFixed(digits)} ${unit}`);
    const rejectedReason = last && !last.accepted ? last.reason : null;
    return html`
      <table>
        <tbody>
          <tr>
            <td>Reported power</td>
            <td class="value">${number(state.power, 2, "W")}</td>
          </tr>
          <tr>
            <td>Last frame</td>
            <td class="value ${last?.accepted ? "ok" : last ? "bad" : ""}">
              ${last ? (last.accepted ? "accepted" : "rejected") : "—"}
            </td>
          </tr>
          ${rejectedReason
            ? html`<tr class="reason">
                <td class="note bad" colspan="2" title=${rejectedReason}>
                  ${rejectedReason}
                </td>
              </tr>`
            : nothing}
          <tr>
            <td>Voltage</td>
            <td class="value">${number(last?.voltage, 1, "V")}</td>
          </tr>
          <tr>
            <td>Current</td>
            <td class="value">${number(last?.current, 3, "A")}</td>
          </tr>
          <tr>
            <td>Power factor</td>
            <td class="value">${number(last?.pf, 3, "")}</td>
          </tr>
          <tr>
            <td>Frame age</td>
            <td class="value">${number(state.frame_age, 1, "s")}</td>
          </tr>
          <tr>
            <td>Camera</td>
            <td class="${state.source_error ? "note bad" : "value"}">
              ${state.source_error ?? number(state.fps, 1, "fps")}
            </td>
          </tr>
          <tr>
            <td>Levelled by</td>
            <td class="value">
              ${state.angle == null
                ? "not located"
                : `${state.angle.toFixed(1)}°`}
            </td>
          </tr>
          <tr>
            <td>Frames</td>
            <td
              class="value"
              title="${state.frames ?? 0} read, ${state.accepted ??
              0} accepted, ${state.rejected ?? 0} rejected"
            >
              ${state.accepted ?? 0}/${state.frames ?? 0} ok ·
              ${state.rejected ?? 0} bad
            </td>
          </tr>
        </tbody>
      </table>
    `;
  }
}
