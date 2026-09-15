import { LitElement, css, html, nothing, svg } from "lit";
import { customElement, property } from "lit/decorators.js";
import type { SweepCoverage, SweepTick, SweepTickStatus } from "../../types";

const HUE_MODULO = 65_536;

@customElement("measure-sweep-map")
export class SweepMap extends LitElement {
  @property({ attribute: false })
  coverage?: SweepCoverage | null;

  /** Pulse the in-progress tick. Off on the result view. */
  @property({ type: Boolean })
  live = false;

  static readonly styles = css`
    :host {
      display: block;
    }
    .map {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 0.55rem 1rem;
      align-items: center;
      max-width: 22rem;
      margin-top: 0.7rem;
    }
    .map.with-hue {
      grid-template-columns: minmax(0, 1fr) auto;
    }
    .gradients {
      display: grid;
      gap: 0.55rem;
      min-width: 0;
    }
    .map > .axis.hue {
      grid-column: 2;
      grid-row: 1 / -1;
    }
    .axis {
      display: grid;
      gap: 0.22rem;
    }
    .axis > span {
      color: var(--muted);
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.1em;
    }
    .track {
      display: grid;
      gap: 0.18rem;
    }
    .bar {
      height: 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
    }
    .bar.ct {
      background: linear-gradient(90deg, #f4a261 0%, #f7e7c6 48%, #9ec9f5 100%);
    }
    .bar.sat {
      background: linear-gradient(90deg, #f4f1ea 0%, #e87ad6 100%);
    }
    .ticks {
      position: relative;
      height: 12px;
    }
    .wheel-wrap {
      display: grid;
      justify-items: start;
    }
    .wheel {
      position: relative;
      width: 4.6rem;
      aspect-ratio: 1;
    }
    .wheel-disk {
      position: absolute;
      inset: 18%;
      border-radius: 50%;
      background: conic-gradient(
        from -90deg,
        hsl(0 90% 55%),
        hsl(60 90% 50%),
        hsl(120 80% 45%),
        hsl(180 80% 45%),
        hsl(240 80% 58%),
        hsl(300 80% 55%),
        hsl(360 90% 55%)
      );
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .tick {
      position: absolute;
      width: 11px;
      height: 11px;
      color: var(--muted);
      transform: translate(-50%, 0);
    }
    .tick svg {
      display: block;
      width: 100%;
      height: 100%;
    }
    .ticks .tick {
      top: 0;
    }
    .wheel .tick {
      left: 50%;
      top: 50%;
      transform: translate(-50%, -50%) rotate(var(--hue-angle)) translateY(-2.05rem) rotate(calc(-1 * var(--hue-angle)));
    }
    .tick.done {
      color: var(--good);
    }
    .tick.inherited {
      color: var(--muted);
      opacity: 0.42;
    }
    .tick.failed {
      color: var(--danger);
    }
    .tick.current,
    .tick.partial {
      color: var(--signal-strong);
    }
    .tick.current.pulse {
      animation: sweep-pulse 1.4s ease-in-out infinite;
    }
    @keyframes sweep-pulse {
      0%,
      100% {
        transform: translate(-50%, 0) scale(1);
      }
      50% {
        transform: translate(-50%, 0) scale(1.22);
      }
    }
    .wheel .tick.current.pulse {
      animation-name: sweep-pulse-wheel;
    }
    @keyframes sweep-pulse-wheel {
      0%,
      100% {
        transform: translate(-50%, -50%) rotate(var(--hue-angle)) translateY(-2.05rem)
          rotate(calc(-1 * var(--hue-angle))) scale(1);
      }
      50% {
        transform: translate(-50%, -50%) rotate(var(--hue-angle)) translateY(-2.05rem)
          rotate(calc(-1 * var(--hue-angle))) scale(1.22);
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .tick.current.pulse {
        animation: none;
      }
    }
  `;

  render() {
    const coverage = this.coverage;
    if (!coverage || !hasSweepAxes(coverage)) return nothing;
    const bars = [
      this.renderBar("Color temp", "ct", "Warm to cool", coverage.color_temp, true),
      this.renderBar("Saturation", "sat", "White to vivid", coverage.saturation, false),
    ];
    const hasBars = Boolean(coverage.color_temp?.length || coverage.saturation?.length);
    return html`
      <div class="map ${coverage.hue?.length ? "with-hue" : ""}" aria-label="Sweep coverage">
        ${hasBars ? html`<div class="gradients">${bars}</div>` : nothing} ${this.renderWheel(coverage.hue)}
      </div>
    `;
  }

  private renderBar(label: string, kind: "ct" | "sat", hint: string, ticks: SweepTick[] | undefined, reverse: boolean) {
    if (!ticks?.length) return nothing;
    const values = ticks.map((tick) => tick.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    return html`
      <div class="axis">
        <span>${label}</span>
        <div class="track">
          <div class="bar ${kind}" role="img" aria-label="${label}. ${hint}."></div>
          <div class="ticks">
            ${ticks.map((tick) => {
              const ratio = (tick.value - min) / span;
              const left = (reverse ? 1 - ratio : ratio) * 100;
              return this.tick(tick, `${label} ${tick.value}`, { left: `${left}%` });
            })}
          </div>
        </div>
      </div>
    `;
  }

  private renderWheel(ticks: SweepTick[] | undefined) {
    if (!ticks?.length) return nothing;
    return html`
      <div class="axis hue">
        <span>Hue</span>
        <div class="wheel-wrap">
          <div class="wheel" role="img" aria-label="Hue wheel">
            <div class="wheel-disk" aria-hidden="true"></div>
            ${ticks.map((tick) => {
              // conic-gradient `from -90deg` puts hsl(0) (red) at 9 o'clock; match that.
              const angle = ((tick.value % HUE_MODULO) / HUE_MODULO) * 360 - 90;
              return this.tick(tick, `Hue ${tick.value}`, { left: "50%", top: "50%", angle });
            })}
          </div>
        </div>
      </div>
    `;
  }

  private tick(tick: SweepTick, label: string, position: { left: string; top?: string; angle?: number }) {
    const status = visibleStatus(tick.status, this.live);
    const angle = position.angle == null ? "" : ` --hue-angle: ${position.angle}deg;`;
    return html`<span
      class="tick ${status}${status === "current" && this.live ? " pulse" : ""}"
      style="left: ${position.left};${position.top ? ` top: ${position.top};` : ""}${angle}"
      title=${`${label}: ${statusLabel(status)}`}
      aria-label=${`${label}, ${statusLabel(status)}`}
      >${tickIcon(status)}</span
    >`;
  }
}

export function hasSweepAxes(coverage?: SweepCoverage | null): boolean {
  return Boolean(coverage?.color_temp?.length || coverage?.hue?.length || coverage?.saturation?.length);
}

/** Result view has no live current tick: leftover in-progress becomes partial. */
export function visibleStatus(status: SweepTickStatus, live: boolean): SweepTickStatus {
  if (status === "current" && !live) return "partial";
  return status;
}

export function statusLabel(status: SweepTickStatus): string {
  return {
    done: "done",
    failed: "failed",
    current: "in progress",
    partial: "partial",
    pending: "planned",
    inherited: "from earlier session",
  }[status];
}

function tickIcon(status: SweepTickStatus) {
  if (status === "done" || status === "inherited") {
    return svg`<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M4.6 8.2 7 10.5l4.5-5.2" fill="none" stroke="#122018" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  }
  if (status === "failed") {
    return svg`<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
  }
  if (status === "current") {
    return svg`<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.2" fill="currentColor"/></svg>`;
  }
  if (status === "partial") {
    return svg`<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8a6.2 6.2 0 0 0 0 12.4Z" fill="currentColor"/></svg>`;
  }
  return svg`<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="2.4 2"/></svg>`;
}
