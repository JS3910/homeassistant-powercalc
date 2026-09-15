import { LitElement, css, html, nothing, type PropertyValues } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import { emit } from "../../utils/events";
import {
  applyYToView,
  clampView,
  crosshairData,
  dataBounds,
  dataToPixel,
  ctBriFromPointId,
  ctMiredFromPointId,
  densityValues,
  brightnessLevels,
  filterPlotToBrightness,
  filterPlotToHsBrightness,
  filterPlotToHue,
  filterPlotToMired,
  filterPlotToSession,
  findPoint,
  holdLinksFromPointId,
  histogramCountForX,
  histogramPiles,
  hoverPointForInterestMark,
  hsBriFromPointId,
  hsHueFromPointId,
  interestCirclePoints,
  isoGroups,
  inPlot,
  inPlotX,
  interestTickHit,
  interestTickPixel,
  matePointId,
  nearestPointByX,
  midpoint,
  nearestBriAt,
  nearestHueAt,
  nearestMiredAt,
  markerDensityScale,
  nearestPoint,
  panView,
  pileColumn,
  snapHistogramPile,
  visibleHistogramPiles,
  pixelToData,
  plotInterestMarks,
  plotInnerFrame,
  plotLayout,
  plotPairOf,
  plotPoints,
  pointerDistance,
  samplesAtNoun,
  viewsEqual,
  wheelZoomFactor,
  yViewsEqual,
  zoomView,
  type IsoLineKey,
  type PlotLayout,
  type PlotView,
  type PlotYRange,
} from "./interaction";
import {
  cameraAtRest,
  clampZoom,
  drawCylinder,
  inCylinderFrame,
  INITIAL_TILT,
  INITIAL_YAW,
  INITIAL_ZOOM,
  nearestCylinderPoint,
  pointWatt,
  projectedPixel,
  tiltFromDrag,
  yawFromDrag,
  zoomFromWheel,
  type CylinderLayout,
} from "./cylinder";
import type {
  PlotHoldDetail,
  PlotPoint,
  PlotPointAction,
  PlotPointActionDetail,
  PlotSpec,
  PlotStat,
  PlotYViewDetail,
} from "../../types";
import { sharedStyles } from "../../styles";
import { THEME_CHANGE_EVENT } from "../../theme";

interface PlotPalette {
  background: string;
  foreground: string;
  muted: string;
  grid: string;
  signal: string;
  warning: string;
}

interface PlotOverlay {
  view?: PlotView | null;
  hover?: { x: number; y: number } | null;
  hoverPoint?: PlotPoint | null;
  hoverTick?: boolean;
  selectedId?: string | null;
  fadeInherited?: boolean;
  isoLines?: IsoLineKey[];
}

@customElement("measure-result-plot")
export class ResultPlot extends LitElement {
  @property({ attribute: false })
  plot!: PlotSpec;

  @property({ type: Boolean })
  partial = false;

  /**
   * Fade seed points copied from a refine source so this run's new readings stand
   * out. Only the live running view turns this on; a finished/stopped/failed
   * result treats every point as part of the recorded set.
   */
  @property({ type: Boolean })
  fadeInherited = false;

  @property({ type: Boolean })
  editable = false;

  /** When set on the HS brightness plot, show only that hue's brightness/power slice. */
  @property({ attribute: false })
  filterHue: number | null = null;

  /** When set on the CT kelvin rail, show that brightness instead of the 100% slice. */
  @property({ attribute: false })
  filterBri: number | null = null;

  /** When set on the CT brightness plot, show only that mired's brightness/power slice. */
  @property({ attribute: false })
  filterMired: number | null = null;

  /** Shared power-axis domain for a linked pair. Null keeps this plot's own Y. */
  @property({ attribute: false })
  sharedY: { minY: number; maxY: number } | null = null;

  /** Live power-axis window from the paired plot's zoom or pan. */
  @property({ attribute: false })
  linkedY: PlotYRange | null = null;

  /** Clicked point id for the pair; both plots highlight the mate and freeze hover-follow. */
  @property({ attribute: false })
  heldPointId: string | null = null;

  /** Plot id that was clicked. Only that card shows the selected-point panel. */
  @property({ attribute: false })
  heldSourcePlotId: string | null = null;

  @property({ type: Boolean })
  linksLocked = false;

  /** Extra points counted on this plot's x-axis (HS rail uses the brightness plot). */
  @property({ attribute: false })
  densityPlot: PlotSpec | null = null;

  @state()
  private sessionOnly = false;

  @state()
  private isoMired = false;

  @state()
  private isoHue = false;

  @state()
  private isoSat = false;

  @state()
  private view: PlotView | null = null;

  @state()
  private hover: { x: number; y: number } | null = null;

  @state()
  private hoverPoint: PlotPoint | null = null;

  @state()
  private hoverTick = false;

  @state()
  private hoverCount: number | null = null;

  @state()
  private selectedId: string | null = null;

  @state()
  private editWatt = "";

  @state()
  private yaw = INITIAL_YAW;

  @state()
  private tilt = INITIAL_TILT;

  @state()
  private zoom = INITIAL_ZOOM;

  @state()
  private cylinderBri: number | null = null;

  private resizeObserver?: ResizeObserver;
  private displayedPlotCache: PlotSpec | null = null;
  private displayedPlotSource?: PlotSpec;
  private displayedPlotCacheKey = "";
  private canvas?: HTMLCanvasElement;
  private histogramCanvas?: HTMLCanvasElement;
  private histogramWheelTarget?: HTMLCanvasElement;
  private layout: PlotLayout | null = null;
  private cylinderLayout: CylinderLayout | null = null;
  private pointers = new Map<number, { x: number; y: number }>();
  private pinchDistance: number | null = null;
  private pinchView: PlotView | null = null;
  private pinchZoom: number | null = null;
  private lastPan: { x: number; y: number } | null = null;
  private panning = false;
  private lastLinkedHue: number | null | undefined = undefined;
  private lastLinkedBri: number | null | undefined = undefined;
  private lastLinkedMired: number | null | undefined = undefined;
  private lastPublishedY: PlotYRange | null = null;
  private histogramDragging = false;
  private histogramPanning = false;
  private histogramLastPan: { x: number } | null = null;
  private histogramMarkX: number | null = null;

  static readonly styles = [
    sharedStyles,
    css`
      :host {
        display: block;
        min-width: 0;
      }
      .plot-card {
        height: 100%;
        padding: 1rem;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: var(--field);
      }
      .plot-head {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 1rem;
        min-height: 5.25rem;
        margin-bottom: 0.75rem;
      }
      h4 {
        margin: 0;
        font-size: 1rem;
      }
      .source {
        display: block;
        margin-top: 0.2rem;
        color: var(--muted);
        font:
          0.68rem/1.3 ui-monospace,
          monospace;
        overflow-wrap: anywhere;
      }
      .plot-tools {
        display: flex;
        flex-direction: row;
        flex-wrap: wrap;
        justify-content: flex-end;
        align-items: center;
        gap: 0.4rem 0.75rem;
        max-width: 16rem;
      }
      .partial {
        display: inline-flex;
        padding: 0.28rem 0.5rem;
        border: 1px solid color-mix(in srgb, var(--signal) 65%, var(--line));
        border-radius: 999px;
        color: var(--signal-strong);
        font-size: 0.68rem;
        white-space: nowrap;
      }
      .session-filter {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        margin: 0;
        color: var(--muted);
        font-size: 0.72rem;
        font-weight: 650;
        line-height: 1.2;
        white-space: nowrap;
        cursor: pointer;
      }
      .session-filter input {
        width: 1rem;
        min-width: 1rem;
        max-width: 1rem;
        height: 1rem;
        min-height: 1rem;
        margin: 0;
        padding: 0;
        accent-color: var(--signal);
      }
      .canvas-wrap {
        position: relative;
      }
      canvas.plot {
        display: block;
        width: 100%;
        min-height: 280px;
        border-radius: 8px;
        background: var(--well);
        touch-action: none;
        cursor: crosshair;
      }
      canvas.plot.cylinder {
        cursor: grab;
      }
      canvas.plot.cylinder.orbiting {
        cursor: grabbing;
      }
      canvas.histogram {
        display: block;
        width: 100%;
        height: 52px;
        min-height: 52px;
        margin-top: 0.35rem;
        border-radius: 8px;
        background: var(--well);
        touch-action: none;
        cursor: grab;
      }
      canvas.histogram.panning {
        cursor: grabbing;
      }
      .bri-slider {
        display: grid;
        gap: 0.35rem;
        margin-top: 0.55rem;
      }
      .bri-slider span {
        color: var(--muted);
        font-size: 0.72rem;
        font-weight: 650;
      }
      .bri-slider input[type="range"] {
        width: 100%;
        margin: 0;
        accent-color: var(--signal);
      }
      .tooltip {
        position: absolute;
        z-index: 2;
        min-width: 11rem;
        max-width: 16rem;
        padding: 0.55rem 0.7rem;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: color-mix(in srgb, var(--surface-raised) 92%, transparent);
        box-shadow: 0 10px 24px rgb(0 0 0 / 28%);
        pointer-events: none;
      }
      .tooltip dl,
      .point-panel dl {
        display: grid;
        gap: 0.28rem;
        margin: 0;
      }
      .tooltip div,
      .point-panel .stat {
        display: flex;
        justify-content: space-between;
        gap: 0.8rem;
      }
      .tooltip dt,
      .point-panel dt {
        color: var(--muted);
        font-size: 0.72rem;
      }
      .tooltip dd,
      .point-panel dd {
        margin: 0;
        font:
          650 0.78rem/1.3 ui-monospace,
          monospace;
      }
      .flag {
        margin: 0.4rem 0 0;
        color: var(--warning);
        font-size: 0.72rem;
      }
      .plot-actions {
        display: flex;
        justify-content: flex-end;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin-top: 0.75rem;
      }
      .plot-download,
      .plot-reset {
        min-height: 36px;
        padding: 0.45rem 0.75rem;
        font-size: 0.72rem;
      }
      .point-panel {
        margin-top: 0.85rem;
        padding: 0.85rem 0.95rem;
        border: 1px solid color-mix(in srgb, var(--signal) 45%, var(--line));
        border-radius: 12px;
        background: color-mix(in srgb, var(--signal) 8%, var(--well));
      }
      .point-panel h5 {
        margin: 0 0 0.55rem;
        font-size: 0.92rem;
      }
      .edit-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 0.55rem;
        align-items: end;
        margin: 0.75rem 0 0.55rem;
      }
      .point-buttons {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
      }
      .point-buttons button {
        min-height: 36px;
        padding: 0.4rem 0.7rem;
        font-size: 0.75rem;
      }
    `,
  ];

  protected willUpdate(changed: PropertyValues<this>): void {
    if (changed.has("linkedY")) this.syncLinkedY(changed.get("linkedY") as PlotYRange | null);
    if (!changed.has("plot")) return;
    if (changed.get("plot")?.id !== this.plot?.id) {
      this.view = null;
      this.sessionOnly = false;
      this.isoMired = false;
      this.isoHue = false;
      this.isoSat = false;
      this.selectedId = null;
      this.hover = null;
      this.hoverPoint = null;
      this.hoverTick = false;
      this.hoverCount = null;
      this.lastLinkedHue = undefined;
      this.lastLinkedBri = undefined;
      this.lastLinkedMired = undefined;
      this.lastPublishedY = null;
      this.yaw = INITIAL_YAW;
      this.tilt = INITIAL_TILT;
      this.zoom = INITIAL_ZOOM;
      this.cylinderBri = null;
      this.cylinderLayout = null;
      return;
    }
    if (this.heldPointId && this.heldSourcePlotId === this.plot.id) {
      const held = findPoint(this.plot, this.heldPointId);
      if (held) this.editWatt = String(pointWatt(held));
    } else if (this.selectedId) {
      const point = findPoint(this.displayedPlot(), this.selectedId);
      if (!point) this.selectedId = null;
      else this.editWatt = String(pointWatt(point));
    }
  }

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener(THEME_CHANGE_EVENT, this.themeChanged);
    this.observeCanvas();
    if (this.hasUpdated) this.draw();
  }

  protected firstUpdated(): void {
    this.canvas = this.renderRoot.querySelector("canvas.plot") ?? undefined;
    this.histogramCanvas = this.renderRoot.querySelector("canvas.histogram") ?? undefined;
    this.canvas?.addEventListener("wheel", this.onWheel, { passive: false });
    this.observeCanvas();
  }

  protected updated(changed: PropertyValues<this>): void {
    if (!this.isConnected) return;
    this.observeCanvas();
    if (this.shouldPaint(changed)) this.draw();
  }

  private observeCanvas(): void {
    if (!this.isConnected || this.resizeObserver || typeof ResizeObserver === "undefined") return;
    const canvas = this.renderRoot.querySelector("canvas.plot") as HTMLCanvasElement | null;
    if (!canvas) return;
    this.resizeObserver = new ResizeObserver(() => {
      this.draw();
    });
    this.resizeObserver.observe(canvas);
  }

  private readonly themeChanged = (): void => {
    if (this.isConnected) this.draw();
  };

  private shouldPaint(changed: PropertyValues<this>): boolean {
    for (const key of changed.keys()) {
      if (key !== "editWatt") return true;
    }
    return false;
  }

  disconnectedCallback(): void {
    window.removeEventListener(THEME_CHANGE_EVENT, this.themeChanged);
    this.canvas?.removeEventListener("wheel", this.onWheel);
    this.histogramWheelTarget?.removeEventListener("wheel", this.onHistogramWheel);
    this.resizeObserver?.disconnect();
    this.resizeObserver = undefined;
    super.disconnectedCallback();
  }

  render() {
    const selected = this.selectedPoint();
    const zoomed = this.isCylinder()
      ? !cameraAtRest({ yaw: this.yaw, tilt: this.tilt, zoom: this.zoom })
      : !viewsEqual(this.currentView(), this.dataView());
    const plot = this.displayedPlot();
    return html`
      <article class="plot-card">
        <div class="plot-head">
          <div>
            <h4>${plot.title}</h4>
            <span class="source">${plot.source}${this.isCylinder() ? " · drag to orbit · scroll to zoom" : ""}</span>
          </div>
          <div class="plot-tools">
            ${this.partial ? html`<span class="partial">Partial result</span>` : ""}
            <label class="session-filter">
              <input type="checkbox" .checked=${this.sessionOnly} @change=${this.onSessionOnlyChange} />
              This session only
            </label>
            ${this.plot.id === "color_temp"
              ? html`
                  <label class="session-filter">
                    <input type="checkbox" .checked=${this.isoMired} @change=${this.onIsoMiredChange} />
                    Iso-CT
                  </label>
                `
              : nothing}
            ${this.plot.id === "hs"
              ? html`
                  <label class="session-filter">
                    <input type="checkbox" .checked=${this.isoHue} @change=${this.onIsoHueChange} />
                    Iso-hue
                  </label>
                  <label class="session-filter">
                    <input type="checkbox" .checked=${this.isoSat} @change=${this.onIsoSatChange} />
                    Iso-sat
                  </label>
                `
              : nothing}
          </div>
        </div>
        <div class="canvas-wrap">
          <canvas
            class="plot ${this.isCylinder() ? "cylinder" : ""} ${this.panning && this.isCylinder() ? "orbiting" : ""}"
            role="img"
            aria-label="${plot.title}: ${this.isCylinder() ? "power by hue and saturation" : `${plot.y_label} by ${plot.x_label}`}"
            @pointerdown=${this.onPointerDown}
            @pointermove=${this.onPointerMove}
            @pointerup=${this.onPointerUp}
            @pointercancel=${this.onPointerUp}
            @pointerleave=${this.onPointerLeave}
          ></canvas>
          ${this.renderTooltip()}
        </div>
        ${this.renderHistogram(plot)} ${this.renderCylinderBriSlider()}
        ${selected ? this.renderPointPanel(selected) : nothing}
        <div class="plot-actions">
          <button class="plot-reset" type="button" ?disabled=${!zoomed && !this.heldPointId} @click=${this.resetView}>
            Reset view
          </button>
          <button class="plot-download" type="button" @click=${this.download}>Download PNG</button>
        </div>
      </article>
    `;
  }

  private renderTooltip() {
    if (!this.hoverPoint || !this.layout) return nothing;
    const selected = this.selectedPoint();
    if (selected && selected.id && this.hoverPoint.id === selected.id) return nothing;
    const pixel = this.cylinderHoverPixel()
      ?? (this.hoverTick
        ? interestTickPixel(this.layout, this.hoverPoint.x)
        : dataToPixel(this.layout, this.hoverPoint.x, this.hoverPoint.y));
    const left = Math.min(this.layout.width - 180, Math.max(8, pixel.x + 14));
    const top = Math.max(8, pixel.y - (this.hoverTick ? 72 : 12));
    return html`
      <div class="tooltip" style="left:${left}px;top:${top}px" role="status">
        ${this.renderStats(this.hoverPoint)}
        ${this.hoverPoint.ignored ? html`<p class="flag">Ignored in profile export</p>` : nothing}
        ${this.fadeInherited && this.hoverPoint.inherited ? html`<p class="flag">From an earlier session</p>` : nothing}
        ${this.hoverPoint.interest ? html`<p class="flag">${this.hoverPoint.interest}</p>` : nothing}
        ${this.hoverCount != null
          ? html`<p class="flag">
              ${this.hoverCount} sample${this.hoverCount === 1 ? "" : "s"} at this ${samplesAtNoun(this.plot)}
            </p>`
          : nothing}
      </div>
    `;
  }

  private renderHistogram(plot: PlotSpec) {
    if (this.isCylinder() || !this.densityXs().length) return nothing;
    const view = this.currentView();
    const value = this.hoverPoint?.x ?? this.hover?.x ?? view.minX;
    return html`
      <canvas
        class="histogram ${this.histogramPanning ? "panning" : ""}"
        role="slider"
        aria-label="Sample density along ${plot.x_label}"
        aria-valuemin=${view.minX}
        aria-valuemax=${view.maxX}
        aria-valuenow=${value}
        @pointerdown=${this.onHistogramPointerDown}
        @pointermove=${this.onHistogramPointerMove}
        @pointerup=${this.onHistogramPointerUp}
        @pointercancel=${this.onHistogramPointerUp}
        @pointerleave=${this.onHistogramPointerLeave}
      ></canvas>
    `;
  }

  private renderCylinderBriSlider() {
    if (!this.isCylinder()) return nothing;
    const levels = this.cylinderBrightnessLevels();
    if (levels.length < 2) return nothing;
    const selected = this.selectedCylinderBri() ?? levels[levels.length - 1]!;
    const index = Math.max(0, levels.indexOf(selected));
    const pct = Math.round((selected / 255) * 100);
    return html`
      <label class="bri-slider">
        <span>Brightness ${pct}% (${selected})</span>
        <input
          type="range"
          min="0"
          max=${levels.length - 1}
          step="1"
          .value=${String(index)}
          aria-label="Brightness slice"
          aria-valuemin=${Math.round((levels[0]! / 255) * 100)}
          aria-valuemax=${Math.round((levels[levels.length - 1]! / 255) * 100)}
          aria-valuenow=${pct}
          aria-valuetext="${pct} percent"
          @input=${this.onCylinderBriInput}
        />
      </label>
    `;
  }

  private renderPointPanel(point: PlotPoint) {
    const canEdit = this.canMutate(point);
    return html`
      <div class="point-panel" role="region" aria-label="Selected measurement">
        <h5>Selected point</h5>
        ${this.renderStats(point)}
        ${point.ignored ? html`<p class="flag">This point is ignored in profile exports.</p>` : nothing}
        ${canEdit
          ? html`
              <div class="edit-row">
                <label>
                  <span>Power (W)</span>
                  <input
                    type="number"
                    min="0"
                    step="any"
                    .value=${this.editWatt}
                    @input=${(event: Event) => {
                      this.editWatt = (event.target as HTMLInputElement).value;
                    }}
                  />
                </label>
                <button type="button" @click=${() => this.emitAction(point, "edit")}>Save value</button>
              </div>
              <div class="point-buttons">
                <button type="button" @click=${() => this.emitAction(point, "fix_outlier")}>Fix outlier</button>
                <button type="button" @click=${() => this.emitAction(point, point.ignored ? "unignore" : "ignore")}>
                  ${point.ignored ? "Include in export" : "Ignore"}
                </button>
                <button class="danger" type="button" @click=${() => this.emitAction(point, "delete")}>Delete</button>
              </div>
            `
          : html`<p class="muted">Editing is available after the measurement stops.</p>`}
      </div>
    `;
  }

  private renderStats(point: PlotPoint) {
    return html`<dl>
      ${pointStats(point, this.displayedPlot()).map(
        (stat) =>
          html`<div class="stat">
            <dt>${stat.label}</dt>
            <dd>${stat.value}</dd>
          </div>`,
      )}
    </dl>`;
  }

  private draw(): void {
    const canvas = this.renderRoot.querySelector("canvas.plot") as HTMLCanvasElement | null;
    if (!this.isConnected || !canvas || !this.plot) return;
    const plot = this.displayedPlot();
    const width = Math.max(320, Math.round(canvas.clientWidth || 800));
    const height = this.isCylinder() ? 400 : width < 520 ? 300 : 360;
    const scale = Math.min(2, globalThis.devicePixelRatio || 1);
    const context = sizeCanvas(canvas, width, height, scale);
    if (!context) return;
    if (this.isCylinder()) {
      this.cylinderLayout = drawCylinder(context, plot, width, height, this.palette(), {
        yaw: this.yaw,
        tilt: this.tilt,
        zoom: this.zoom,
        hoverPoint: this.hoverPoint,
        selectedId: this.highlightId(),
        fadeInherited: this.fadeInherited,
        domainPoints: plotPoints(this.cylinderDomainPlot()),
      });
      this.layout = null;
      this.histogramCanvas = undefined;
      return;
    }
    const overlay: PlotOverlay = {
      view: this.currentView(),
      hover: this.hoverTick ? null : this.hover,
      hoverPoint: this.hoverPoint,
      hoverTick: this.hoverTick,
      selectedId: this.highlightId(),
      fadeInherited: this.fadeInherited,
      isoLines: this.activeIsoLines(),
    };
    this.layout = drawPlot(context, plot, width, height, this.palette(), overlay);
    this.histogramCanvas = this.renderRoot.querySelector("canvas.histogram") ?? undefined;
    this.syncHistogramWheel(this.histogramCanvas ?? null);
    this.drawHistogram();
  }

  private drawHistogram(): void {
    const canvas = this.histogramCanvas;
    const layout = this.layout;
    if (!canvas || !layout) return;
    const values = this.densityXs();
    if (!values.length) return;
    const width = layout.width;
    const height = 52;
    const scale = Math.min(2, globalThis.devicePixelRatio || 1);
    const context = sizeCanvas(canvas, width, height, scale);
    if (!context) return;
    const palette = this.palette();
    const inner = plotInnerFrame(layout);
    const left = inner.left;
    const plotWidth = inner.plotWidth;
    const view = layout.view;
    const piles = visibleHistogramPiles(histogramPiles(values), view.minX, view.maxX, plotWidth);
    const peak = Math.max(1, ...piles.map((pile) => pile.count));
    const top = 6;
    const bottom = height - 8;
    const barHeight = bottom - top;
    context.clearRect(0, 0, width, height);
    context.fillStyle = palette.background;
    context.fillRect(0, 0, width, height);
    for (const pile of piles) {
      const column = pileColumn(pile.x, view.minX, view.maxX, plotWidth);
      const h = Math.max(2, (pile.count / peak) * barHeight);
      context.fillStyle = palette.signal;
      context.globalAlpha = 0.72;
      context.fillRect(left + column, bottom - h, 1, h);
    }
    const selected = this.histogramMarkX ?? this.hoverPoint?.x ?? this.hover?.x;
    if (selected != null && selected >= view.minX && selected <= view.maxX) {
      const x = left + pileColumn(selected, view.minX, view.maxX, plotWidth);
      context.globalAlpha = 1;
      context.fillStyle = palette.warning;
      context.beginPath();
      context.moveTo(x, top);
      context.lineTo(x - 5, top + 8);
      context.lineTo(x + 5, top + 8);
      context.closePath();
      context.fill();
      context.strokeStyle = palette.warning;
      context.globalAlpha = 0.85;
      context.beginPath();
      context.moveTo(x + 0.5, top + 8);
      context.lineTo(x + 0.5, bottom);
      context.stroke();
    }
    context.globalAlpha = 1;
    context.fillStyle = palette.muted;
    context.font = '11px ui-monospace, "SFMono-Regular", monospace';
    context.textBaseline = "bottom";
    context.fillText("Samples", 8, height - 4);
  }

  private readonly download = (): void => {
    const canvas = document.createElement("canvas");
    canvas.width = 1200;
    canvas.height = 720;
    const context = canvas.getContext("2d");
    if (!context) return;
    if (this.isCylinder()) {
      drawCylinder(context, this.displayedPlot(), canvas.width, canvas.height, this.palette(), {
        yaw: this.yaw,
        tilt: this.tilt,
        zoom: this.zoom,
        fadeInherited: this.fadeInherited,
        domainPoints: plotPoints(this.cylinderDomainPlot()),
      });
    } else {
      drawPlot(context, this.displayedPlot(), canvas.width, canvas.height, this.palette(), {
        view: this.view,
        fadeInherited: this.fadeInherited,
        isoLines: this.activeIsoLines(),
      });
    }
    const anchor = document.createElement("a");
    anchor.href = canvas.toDataURL("image/png");
    anchor.download = `${this.plot.id}.png`;
    anchor.rel = "noopener";
    anchor.style.display = "none";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  };

  private readonly resetView = (): void => {
    this.view = null;
    this.yaw = INITIAL_YAW;
    this.tilt = INITIAL_TILT;
    this.zoom = INITIAL_ZOOM;
    this.histogramMarkX = null;
    this.hover = null;
    this.hoverPoint = null;
    this.hoverTick = false;
    this.hoverCount = null;
    if (this.heldPointId) this.emitHold(null);
    this.publishYView();
  };

  private readonly onSessionOnlyChange = (event: Event): void => {
    this.sessionOnly = (event.target as HTMLInputElement).checked;
    if (this.sessionOnly && this.hoverPoint?.inherited) {
      this.hoverPoint = null;
      this.hoverTick = false;
    }
  };

  private readonly onIsoMiredChange = (event: Event): void => {
    this.isoMired = (event.target as HTMLInputElement).checked;
  };

  private readonly onIsoHueChange = (event: Event): void => {
    this.isoHue = (event.target as HTMLInputElement).checked;
  };

  private readonly onIsoSatChange = (event: Event): void => {
    this.isoSat = (event.target as HTMLInputElement).checked;
  };

  private readonly onCylinderBriInput = (event: Event): void => {
    const levels = this.cylinderBrightnessLevels();
    const index = Number((event.target as HTMLInputElement).value);
    this.cylinderBri = levels[index] ?? null;
    if (this.hoverPoint && hsBriFromPointId(this.hoverPoint.id) !== this.cylinderBri) {
      this.hoverPoint = null;
      this.hoverTick = false;
      this.hoverCount = null;
    }
    if (this.selectedId && hsBriFromPointId(this.selectedId) !== this.cylinderBri) {
      this.selectedId = null;
    }
  };

  private activeIsoLines(): IsoLineKey[] {
    const keys: IsoLineKey[] = [];
    if (this.plot.id === "color_temp" && this.isoMired) keys.push("mired");
    if (this.plot.id === "hs" && this.isoSat) keys.push("sat");
    if (this.plot.id === "hs" && this.isoHue) keys.push("hue");
    return keys;
  }

  private syncHistogramWheel(canvas: HTMLCanvasElement | null): void {
    if (this.histogramWheelTarget === canvas) return;
    this.histogramWheelTarget?.removeEventListener("wheel", this.onHistogramWheel);
    this.histogramWheelTarget = canvas ?? undefined;
    canvas?.addEventListener("wheel", this.onHistogramWheel, { passive: false });
  }

  private readonly onWheel = (event: WheelEvent): void => {
    if (this.isCylinder()) {
      if (!this.cylinderLayout || !inCylinderFrame(this.cylinderLayout, event.offsetX, event.offsetY)) {
        return;
      }
      event.preventDefault();
      this.zoom = zoomFromWheel(this.zoom, event.deltaY);
      return;
    }
    if (!this.layout || !inPlot(this.layout, event.offsetX, event.offsetY)) return;
    event.preventDefault();
    const origin = pixelToData(this.layout, event.offsetX, event.offsetY);
    this.applyZoom(wheelZoomFactor(event.deltaY), origin.x, origin.y);
  };

  private readonly onHistogramWheel = (event: WheelEvent): void => {
    if (!this.layout || !inPlotX(this.layout, event.offsetX)) return;
    event.preventDefault();
    const view = this.currentView();
    const originX = pixelToData(this.layout, event.offsetX, this.layout.top).x;
    this.applyZoom(wheelZoomFactor(event.deltaY), originX, (view.minY + view.maxY) / 2);
  };

  private readonly onPointerDown = (event: PointerEvent): void => {
    (event.currentTarget as HTMLCanvasElement).setPointerCapture(event.pointerId);
    this.pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });
    this.lastPan = { x: event.offsetX, y: event.offsetY };
    this.panning = false;
    if (this.pointers.size === 2) {
      const [first, second] = [...this.pointers.values()];
      if (first && second) {
        this.pinchDistance = pointerDistance(first, second);
        this.pinchView = this.currentView();
        this.pinchZoom = this.zoom;
      }
    }
  };

  private readonly onPointerMove = (event: PointerEvent): void => {
    if (this.pointers.has(event.pointerId)) {
      this.pointers.set(event.pointerId, {
        x: event.offsetX,
        y: event.offsetY,
      });
    }
    if (this.isCylinder()) {
      if (this.pointers.size === 2 && this.pinchDistance && this.pinchZoom != null) {
        const [first, second] = [...this.pointers.values()];
        if (first && second) {
          const distance = pointerDistance(first, second);
          if (distance >= 1) this.zoom = clampZoom(this.pinchZoom * (distance / this.pinchDistance));
        }
        this.hover = null;
        this.hoverPoint = null;
        this.hoverTick = false;
        this.hoverCount = null;
        return;
      }
      if (this.pointers.size === 1 && this.lastPan && this.cylinderLayout) {
        const dx = event.offsetX - this.lastPan.x;
        const dy = event.offsetY - this.lastPan.y;
        if (!this.panning && Math.hypot(dx, dy) > 8) this.panning = true;
        if (this.panning) {
          this.yaw = yawFromDrag(this.yaw, dx, this.cylinderLayout.frame.plotWidth);
          this.tilt = tiltFromDrag(this.tilt, dy, this.cylinderLayout.frame.plotHeight);
          this.lastPan = { x: event.offsetX, y: event.offsetY };
          this.hover = null;
          this.hoverPoint = null;
          this.hoverTick = false;
          this.hoverCount = null;
          this.publishPlotLinks();
          return;
        }
      }
      this.updateHover(event.offsetX, event.offsetY);
      return;
    }
    if (this.pointers.size === 2 && this.pinchDistance && this.pinchView && this.layout) {
      const [first, second] = [...this.pointers.values()];
      if (!first || !second) return;
      const distance = pointerDistance(first, second);
      if (distance < 1) return;
      const center = midpoint(first, second);
      const origin = pixelToData(this.layout, center.x, center.y);
      this.view = clampView(
        zoomView(this.pinchView, distance / this.pinchDistance, origin.x, origin.y),
        this.dataView(),
      );
      this.publishYView();
      return;
    }
    if (this.pointers.size === 1 && this.lastPan && this.layout) {
      const dx = event.offsetX - this.lastPan.x;
      const dy = event.offsetY - this.lastPan.y;
      if (!this.panning && Math.hypot(dx, dy) > 8) this.panning = true;
      if (this.panning) {
        const from = pixelToData(this.layout, this.lastPan.x, this.lastPan.y);
        const to = pixelToData(this.layout, event.offsetX, event.offsetY);
        this.view = clampView(panView(this.currentView(), to.x - from.x, to.y - from.y), this.dataView());
        this.lastPan = { x: event.offsetX, y: event.offsetY };
        this.hover = null;
        this.hoverPoint = null;
        this.hoverTick = false;
        this.hoverCount = null;
        this.publishYView();
        this.publishPlotLinks();
        return;
      }
    }
    this.updateHover(event.offsetX, event.offsetY);
  };

  private readonly onPointerUp = (event: PointerEvent): void => {
    const start = this.pointers.get(event.pointerId);
    this.pointers.delete(event.pointerId);
    const wasPanning = this.panning;
    if (this.pointers.size < 2) {
      this.pinchDistance = null;
      this.pinchView = null;
      this.pinchZoom = null;
    }
    if (this.pointers.size === 0) {
      this.lastPan = null;
      this.panning = false;
    }
    if (!start || this.pointers.size > 0 || wasPanning) return;
    if (pointerDistance(start, { x: event.offsetX, y: event.offsetY }) > 8) return;
    this.selectAt(event.offsetX, event.offsetY);
  };

  private readonly onPointerLeave = (): void => {
    if (this.pointers.size) return;
    this.hover = null;
    this.hoverPoint = null;
    this.hoverTick = false;
    this.hoverCount = null;
    this.histogramMarkX = null;
    this.publishPlotLinks();
  };

  private readonly onHistogramPointerDown = (event: PointerEvent): void => {
    (event.currentTarget as HTMLCanvasElement).setPointerCapture(event.pointerId);
    this.histogramDragging = true;
    this.histogramPanning = false;
    this.histogramLastPan = { x: event.offsetX };
  };

  private readonly onHistogramPointerMove = (event: PointerEvent): void => {
    if (this.histogramDragging && this.histogramLastPan && this.layout) {
      const dx = event.offsetX - this.histogramLastPan.x;
      if (!this.histogramPanning && Math.abs(dx) > 8) this.histogramPanning = true;
      if (this.histogramPanning) {
        const from = pixelToData(this.layout, this.histogramLastPan.x, this.layout.top);
        const to = pixelToData(this.layout, event.offsetX, this.layout.top);
        this.view = clampView(panView(this.currentView(), to.x - from.x, 0), this.dataView());
        this.histogramLastPan = { x: event.offsetX };
        this.hover = null;
        this.hoverPoint = null;
        this.hoverTick = false;
        this.hoverCount = null;
        this.histogramMarkX = null;
        this.publishYView();
        this.publishPlotLinks();
        return;
      }
    }
    this.applyHistogramX(event.offsetX, false);
  };

  private readonly onHistogramPointerUp = (event: PointerEvent): void => {
    const wasPanning = this.histogramPanning;
    this.histogramDragging = false;
    this.histogramPanning = false;
    this.histogramLastPan = null;
    if (wasPanning) {
      this.requestUpdate();
      return;
    }
    this.applyHistogramX(event.offsetX, true);
  };

  private readonly onHistogramPointerLeave = (): void => {
    if (this.histogramDragging) return;
    this.hoverCount = null;
  };

  private applyHistogramX(pixelX: number, pin: boolean): void {
    if (!this.layout) return;
    const x = pixelToData(this.layout, pixelX, this.layout.top).x;
    const pile = snapHistogramPile(
      histogramPiles(this.densityXs()),
      x,
      this.layout.view.minX,
      this.layout.view.maxX,
      this.layout.plotWidth,
    );
    this.hoverCount = pile?.count ?? null;
    this.histogramMarkX = pile?.x ?? null;
    const point = pile ? nearestPointByX(plotPoints(this.displayedPlot()), pile.x) : null;
    if (!point) {
      this.hover = { x, y: this.layout.view.minY };
      this.hoverPoint = null;
      this.hoverTick = true;
      if (pin) this.emitHold(null);
      this.publishPlotLinks();
      return;
    }
    this.hover = { x: point.x, y: point.y };
    this.hoverPoint = point;
    this.hoverTick = true;
    if (pin && point.id) {
      this.selectedId = point.id;
      this.editWatt = String(point.y);
      this.emitHold(point.id);
      return;
    }
    this.publishPlotLinks();
  }

  private densityXs(): number[] {
    const source = filterPlotToSession(this.densityPlot ?? this.plot, this.sessionOnly);
    return densityValues(this.plot, source);
  }

  private currentHistogramPiles() {
    return histogramPiles(this.densityXs());
  }

  private interestMarkAt(pixelX: number, pixelY: number) {
    if (!this.layout) return null;
    return interestTickHit(this.layout, plotInterestMarks(this.displayedPlot()), pixelX, pixelY);
  }

  private updateHover(pixelX: number, pixelY: number): void {
    if (this.isCylinder()) {
      if (!this.cylinderLayout || !inCylinderFrame(this.cylinderLayout, pixelX, pixelY)) {
        this.hover = null;
        this.hoverPoint = null;
        this.hoverTick = false;
        this.hoverCount = null;
        this.publishPlotLinks();
        return;
      }
      this.hoverTick = false;
      this.hover = null;
      this.hoverPoint = nearestCylinderPoint(
        plotPoints(this.displayedPlot()),
        this.cylinderLayout,
        pixelX,
        pixelY,
      );
      this.publishPlotLinks();
      return;
    }
    if (!this.layout) return;
    const mark = this.interestMarkAt(pixelX, pixelY);
    if (mark) {
      this.hoverTick = true;
      this.hover = { x: mark.x, y: this.layout.view.minY };
      this.hoverPoint = hoverPointForInterestMark(this.displayedPlot(), mark);
      this.histogramMarkX = mark.x;
      this.hoverCount = histogramCountForX(this.currentHistogramPiles(), mark.x) || null;
      this.publishPlotLinks();
      return;
    }
    if (!inPlot(this.layout, pixelX, pixelY)) {
      this.hover = null;
      this.hoverPoint = null;
      this.hoverTick = false;
      this.hoverCount = null;
      this.histogramMarkX = null;
      this.publishPlotLinks();
      return;
    }
    this.hoverTick = false;
    this.hover = pixelToData(this.layout, pixelX, pixelY);
    this.hoverPoint = nearestPoint(plotPoints(this.displayedPlot()), this.layout, pixelX, pixelY);
    this.histogramMarkX = this.hoverPoint?.x ?? this.hover.x;
    this.hoverCount = histogramCountForX(this.currentHistogramPiles(), this.histogramMarkX) || null;
    this.publishPlotLinks();
  }

  private selectAt(pixelX: number, pixelY: number): void {
    if (this.isCylinder()) {
      if (!this.cylinderLayout || !inCylinderFrame(this.cylinderLayout, pixelX, pixelY)) {
        this.emitHold(null);
        return;
      }
      const point = nearestCylinderPoint(
        plotPoints(this.displayedPlot()),
        this.cylinderLayout,
        pixelX,
        pixelY,
      );
      if (!point?.id) {
        this.emitHold(null);
        return;
      }
      this.selectPoint(point);
      return;
    }
    if (!this.layout) {
      this.emitHold(null);
      return;
    }
    const mark = this.interestMarkAt(pixelX, pixelY);
    const marked = mark ? hoverPointForInterestMark(this.displayedPlot(), mark) : null;
    if (marked?.id) {
      this.selectPoint(marked);
      return;
    }
    if (!inPlot(this.layout, pixelX, pixelY)) {
      this.emitHold(null);
      return;
    }
    const point = nearestPoint(plotPoints(this.displayedPlot()), this.layout, pixelX, pixelY);
    if (!point?.id) {
      this.emitHold(null);
      return;
    }
    this.selectPoint(point);
  }

  private selectPoint(point: PlotPoint): void {
    if (!point.id) {
      this.emitHold(null);
      return;
    }
    if (this.heldPointId === point.id && this.heldSourcePlotId === this.plot.id) {
      this.emitHold(null);
      return;
    }
    this.selectedId = point.id;
    this.editWatt = String(pointWatt(point));
    this.hoverPoint = point;
    this.hoverTick = false;
    this.emitHold(point.id);
  }

  private emitHold(pointId: string | null): void {
    this.selectedId = pointId;
    if (!pointId) {
      this.editWatt = "";
      emit<PlotHoldDetail | null>(this, "plot-hold", null);
      return;
    }
    emit<PlotHoldDetail | null>(this, "plot-hold", {
      pointId,
      plotId: this.plot.id,
      ...holdLinksFromPointId(pointId),
    });
  }

  private applyZoom(factor: number, originX: number, originY: number): void {
    this.view = clampView(zoomView(this.currentView(), factor, originX, originY), this.dataView());
    this.publishYView();
  }

  private syncLinkedY(previous: PlotYRange | null): void {
    if (this.linkedY) {
      const next = clampView(applyYToView(this.view ?? this.dataView(), this.linkedY), this.dataView());
      if (!this.view || !yViewsEqual(this.view, this.linkedY)) this.view = next;
      return;
    }
    if (!previous || !this.view) return;
    const data = this.dataView();
    const next = applyYToView(this.view, { minY: data.minY, maxY: data.maxY });
    this.view = viewsEqual(next, data) ? null : next;
  }

  private publishYView(): void {
    const pair = plotPairOf(this.plot.id);
    if (!pair) return;
    const y = this.view ? { minY: this.view.minY, maxY: this.view.maxY } : null;
    if (y == null) {
      if (this.lastPublishedY == null) return;
    } else if (this.lastPublishedY && yViewsEqual(y, this.lastPublishedY)) {
      return;
    }
    this.lastPublishedY = y;
    emit<PlotYViewDetail>(this, "plot-y-view", { pair, y });
  }

  private dataView(): PlotView {
    const bounds = dataBounds(this.plot);
    if (!this.sharedY) return bounds;
    return { ...bounds, minY: this.sharedY.minY, maxY: this.sharedY.maxY };
  }

  private currentView(): PlotView {
    return this.view ?? this.dataView();
  }

  private highlightId(): string | null {
    if (!this.heldPointId) return this.selectedId;
    return matePointId(this.displayedPlot(), this.heldPointId) ?? this.heldPointId;
  }

  private selectedPoint(): PlotPoint | null {
    const id = this.heldPointId && this.heldSourcePlotId === this.plot.id ? this.heldPointId : this.selectedId;
    return id ? findPoint(this.displayedPlot(), id) : null;
  }

  private displayedPlot(): PlotSpec {
    const key = `${this.sessionOnly}\0${this.filterHue}\0${this.filterMired}\0${this.filterBri}\0${this.cylinderBri}`;
    if (this.displayedPlotCache && this.displayedPlotSource === this.plot && this.displayedPlotCacheKey === key) {
      return this.displayedPlotCache;
    }
    const next = filterPlotToHsBrightness(
      filterPlotToSession(
        filterPlotToBrightness(
          filterPlotToMired(filterPlotToHue(this.plot, this.filterHue), this.filterMired),
          this.filterBri,
        ),
        this.sessionOnly,
      ),
      this.cylinderBri,
    );
    this.displayedPlotSource = this.plot;
    this.displayedPlotCache = next;
    this.displayedPlotCacheKey = key;
    return next;
  }

  private cylinderDomainPlot(): PlotSpec {
    return filterPlotToSession(this.plot, this.sessionOnly);
  }

  private cylinderBrightnessLevels(): number[] {
    return brightnessLevels(this.cylinderDomainPlot());
  }

  private selectedCylinderBri(): number | null {
    const levels = this.cylinderBrightnessLevels();
    if (!levels.length) return null;
    if (this.cylinderBri != null && levels.includes(this.cylinderBri)) return this.cylinderBri;
    return levels[levels.length - 1]!;
  }

  private publishPlotLinks(): void {
    if (this.linksLocked) return;
    this.publishHueLink();
    this.publishBriLink();
    this.publishMiredLink();
  }

  private publishHueLink(): void {
    if (this.plot.id !== "hs_max_bri" && this.plot.id !== "hs_cylinder") return;
    const hue =
      hsHueFromPointId(this.hoverPoint?.id) ??
      (this.hover ? nearestHueAt(plotPoints(this.displayedPlot()), this.hover.x) : null);
    if (this.lastLinkedHue === hue) return;
    this.lastLinkedHue = hue;
    emit<{ hue: number | null }>(this, "plot-hue-link", { hue });
  }

  private publishBriLink(): void {
    if (this.plot.id !== "color_temp") return;
    const bri =
      ctBriFromPointId(this.hoverPoint?.id) ??
      (this.hover ? nearestBriAt(plotPoints(this.displayedPlot()), this.hover.x) : null);
    if (this.lastLinkedBri === bri) return;
    this.lastLinkedBri = bri;
    emit<{ bri: number | null }>(this, "plot-bri-link", { bri });
  }

  private publishMiredLink(): void {
    if (this.plot.id !== "color_temp_max_bri") return;
    const mired =
      ctMiredFromPointId(this.hoverPoint?.id) ??
      (this.hover ? nearestMiredAt(plotPoints(this.displayedPlot()), this.hover.x) : null);
    if (this.lastLinkedMired === mired) return;
    this.lastLinkedMired = mired;
    emit<{ mired: number | null }>(this, "plot-mired-link", { mired });
  }

  private isCylinder(): boolean {
    return this.plot?.kind === "cylinder";
  }

  private cylinderHoverPixel(): { x: number; y: number } | null {
    if (!this.isCylinder() || !this.hoverPoint || !this.cylinderLayout) return null;
    return projectedPixel(this.cylinderLayout, this.hoverPoint);
  }

  private canMutate(point: PlotPoint): boolean {
    return this.editable && Boolean(point.id) && point.editable !== false;
  }

  private emitAction(point: PlotPoint, action: PlotPointAction): void {
    if (!point.id || !this.canMutate(point)) return;
    const detail: PlotPointActionDetail = { pointId: point.id, action };
    if (action === "edit") {
      const watt = Number(this.editWatt);
      if (!Number.isFinite(watt) || watt < 0) return;
      detail.watt = watt;
    }
    if (action === "delete") this.emitHold(null);
    emit<PlotPointActionDetail>(this, "plot-point-action", detail);
  }

  private palette(): PlotPalette {
    const style = getComputedStyle(this);
    const value = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
    return {
      background: value("--well", "#0a0e15"),
      foreground: value("--ink", "#eef2f7"),
      muted: value("--muted", "#93a1b5"),
      grid: value("--grid", "#55647a"),
      signal: value("--signal", "#5488e8"),
      warning: value("--warning", "#f2b84b"),
    };
  }
}

function sizeCanvas(
  canvas: HTMLCanvasElement,
  width: number,
  height: number,
  scale: number,
): CanvasRenderingContext2D | null {
  const nextWidth = Math.round(width * scale);
  const nextHeight = Math.round(height * scale);
  if (canvas.width !== nextWidth || canvas.height !== nextHeight) {
    canvas.width = nextWidth;
    canvas.height = nextHeight;
  }
  const cssHeight = `${height}px`;
  if (canvas.style.height !== cssHeight) canvas.style.height = cssHeight;
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.setTransform(scale, 0, 0, scale, 0, 0);
  return context;
}

export function drawPlot(
  context: CanvasRenderingContext2D,
  plot: PlotSpec,
  width: number,
  height: number,
  palette: PlotPalette,
  overlay: PlotOverlay = {},
): PlotLayout {
  const layout = plotLayout(plot, width, height, overlay.view);
  const frame = {
    left: layout.left,
    top: layout.top,
    plotWidth: layout.plotWidth,
    plotHeight: layout.plotHeight,
    x: (value: number) => dataToPixel(layout, value, layout.view.minY).x,
    y: (value: number) => dataToPixel(layout, layout.view.minX, value).y,
  };

  context.clearRect(0, 0, width, height);
  context.fillStyle = palette.background;
  context.fillRect(0, 0, width, height);
  context.font = '12px ui-monospace, "SFMono-Regular", monospace';
  context.lineWidth = 1;
  context.textBaseline = "middle";

  drawGrid(context, frame, [layout.view.minY, layout.view.maxY], palette);
  drawXTicks(context, frame, [layout.view.minX, layout.view.maxX], palette);

  context.fillStyle = palette.foreground;
  context.fillText(plot.x_label, layout.left + layout.plotWidth / 2, height - 14);
  context.save();
  context.translate(16, layout.top + layout.plotHeight / 2);
  context.rotate(-Math.PI / 2);
  context.fillText(plot.y_label, 0, 0);
  context.restore();

  context.save();
  context.beginPath();
  context.rect(frame.left, frame.top, frame.plotWidth, frame.plotHeight);
  context.clip();
  drawIsoLines(context, plot, frame, palette, overlay.isoLines ?? []);
  const rail = overlay.hoverPoint?.rail ?? null;
  const densityScale = markerDensityScale(
    plot.series.flatMap((series) =>
      series.points
        .filter(
          (point) =>
            point.x >= layout.view.minX &&
            point.x <= layout.view.maxX &&
            point.y >= layout.view.minY &&
            point.y <= layout.view.maxY,
        )
        .map((point) => ({ x: frame.x(point.x), y: frame.y(point.y) })),
    ),
  );
  for (const series of plot.series) {
    drawSeries(
      context,
      plot.kind,
      series,
      frame,
      palette,
      rail,
      overlay.hoverPoint ?? null,
      overlay.selectedId ?? null,
      overlay.fadeInherited === true,
      densityScale,
    );
  }
  const crosshair = crosshairData(overlay.hover ?? null, overlay.hoverPoint);
  if (crosshair && inPlot(layout, frame.x(crosshair.x), frame.y(crosshair.y))) {
    drawCrosshair(context, frame, crosshair, palette);
  }
  context.restore();
  drawInterestTicks(context, frame, plot, layout.view, palette, overlay);
  drawInterestCircles(context, frame, plot, layout.view, palette);
  return layout;
}

function drawCrosshair(
  context: CanvasRenderingContext2D,
  frame: {
    left: number;
    top: number;
    plotWidth: number;
    plotHeight: number;
    x: (value: number) => number;
    y: (value: number) => number;
  },
  hover: { x: number; y: number },
  palette: PlotPalette,
): void {
  const x = frame.x(hover.x);
  const y = frame.y(hover.y);
  context.save();
  context.strokeStyle = palette.foreground;
  context.globalAlpha = 0.35;
  context.setLineDash([4, 4]);
  context.beginPath();
  context.moveTo(x, frame.top);
  context.lineTo(x, frame.top + frame.plotHeight);
  context.moveTo(frame.left, y);
  context.lineTo(frame.left + frame.plotWidth, y);
  context.stroke();
  context.setLineDash([]);
  context.globalAlpha = 1;
  context.fillStyle = palette.signal;
  context.beginPath();
  context.arc(x, frame.top + frame.plotHeight, 3.5, 0, Math.PI * 2);
  context.arc(frame.left, y, 3.5, 0, Math.PI * 2);
  context.fill();
  context.restore();
}

function drawGrid(
  context: CanvasRenderingContext2D,
  frame: { left: number; top: number; plotWidth: number; plotHeight: number },
  [minY, maxY]: [number, number],
  palette: PlotPalette,
): void {
  const step = niceStep(maxY - minY, 5);
  for (const tick of niceTicks(minY, maxY, step)) {
    const ratio = maxY === minY ? 0 : (tick - minY) / (maxY - minY);
    const gridY = frame.top + (1 - ratio) * frame.plotHeight;
    context.strokeStyle = palette.grid;
    context.globalAlpha = 0.35;
    context.beginPath();
    context.moveTo(frame.left, gridY);
    context.lineTo(frame.left + frame.plotWidth, gridY);
    context.stroke();
    context.globalAlpha = 1;
    context.fillStyle = palette.muted;
    context.textAlign = "right";
    context.fillText(formatTick(tick, step), frame.left - 9, gridY);
  }
}

function visibleInterestMarks(plot: PlotSpec, view: PlotView): { x: number; label: string }[] {
  return plotInterestMarks(plot).filter((mark) => mark.x >= view.minX && mark.x <= view.maxX);
}

function drawIsoLines(
  context: CanvasRenderingContext2D,
  plot: PlotSpec,
  frame: { x: (value: number) => number; y: (value: number) => number },
  palette: PlotPalette,
  keys: readonly IsoLineKey[],
): void {
  const points = plot.series.flatMap((series) => series.points);
  for (const key of keys) {
    const dashed = key === "sat";
    for (const group of isoGroups(points, key)) {
      context.save();
      context.strokeStyle = group[0]?.color || palette.signal;
      context.globalAlpha = dashed ? 0.16 : 0.22;
      context.lineWidth = 1;
      if (dashed) context.setLineDash([3, 3]);
      context.beginPath();
      group.forEach((point, index) => {
        const x = frame.x(point.x);
        const y = frame.y(point.y);
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      });
      context.stroke();
      context.restore();
    }
  }
}

function drawInterestCircles(
  context: CanvasRenderingContext2D,
  frame: {
    x: (value: number) => number;
    y: (value: number) => number;
  },
  plot: PlotSpec,
  view: PlotView,
  palette: PlotPalette,
): void {
  for (const point of interestCirclePoints(plot)) {
    if (point.x < view.minX || point.x > view.maxX) continue;
    const px = frame.x(point.x);
    const py = frame.y(point.y);
    context.save();
    context.strokeStyle = palette.warning;
    context.globalAlpha = 0.4;
    context.lineWidth = 1.4;
    context.beginPath();
    context.arc(px, py, 9, 0, Math.PI * 2);
    context.stroke();
    context.restore();
  }
}

function drawInterestTicks(
  context: CanvasRenderingContext2D,
  frame: {
    left: number;
    top: number;
    plotWidth: number;
    plotHeight: number;
    x: (value: number) => number;
  },
  plot: PlotSpec,
  view: PlotView,
  palette: PlotPalette,
  overlay: PlotOverlay,
): void {
  const marks = visibleInterestMarks(plot, view);
  if (!marks.length) return;
  const topY = frame.top;
  const axisY = frame.top + frame.plotHeight;
  const hoverX = overlay.hoverTick ? overlay.hoverPoint?.x : null;
  for (const mark of marks) {
    const x = frame.x(mark.x);
    const hovered = hoverX != null && Math.abs(hoverX - mark.x) <= 1e-6;
    const half = hovered ? 6 : 4.5;
    const depth = hovered ? 10 : 8;
    context.save();
    context.fillStyle = palette.warning;
    context.globalAlpha = hovered ? 1 : 0.95;
    context.beginPath();
    context.moveTo(x, axisY);
    context.lineTo(x - half, axisY + depth);
    context.lineTo(x + half, axisY + depth);
    context.closePath();
    context.fill();
    context.beginPath();
    context.moveTo(x, topY);
    context.lineTo(x - half, topY - depth);
    context.lineTo(x + half, topY - depth);
    context.closePath();
    context.fill();
    context.restore();
  }
}

function drawXTicks(
  context: CanvasRenderingContext2D,
  frame: {
    left: number;
    top: number;
    plotWidth: number;
    plotHeight: number;
    x: (value: number) => number;
  },
  [minX, maxX]: [number, number],
  palette: PlotPalette,
): void {
  const step = niceStep(maxX - minX, 10);
  const ticks = niceTicks(minX, maxX, step);
  const minLabelSpacing = 28;
  const showLabels = ticks.length <= 1 || frame.plotWidth / (ticks.length - 1) >= minLabelSpacing;
  for (const tick of ticks) {
    const gridX = frame.x(tick);
    context.strokeStyle = palette.grid;
    context.globalAlpha = 0.35;
    context.beginPath();
    context.moveTo(gridX, frame.top);
    context.lineTo(gridX, frame.top + frame.plotHeight);
    context.stroke();
    context.globalAlpha = 1;
    if (showLabels) {
      context.fillStyle = palette.muted;
      context.textAlign = "center";
      context.fillText(formatTick(tick, step), gridX, frame.top + frame.plotHeight + 20);
    }
  }
}

function niceStep(range: number, targetCount: number): number {
  if (!(range > 0)) return 1;
  const roughStep = range / targetCount;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const residual = roughStep / magnitude;
  const niceResidual = residual >= 5 ? 10 : residual >= 2 ? 5 : residual >= 1 ? 2 : 1;
  return niceResidual * magnitude;
}

function niceTicks(min: number, max: number, step: number): number[] {
  if (!(max > min)) return [min];
  const start = Math.ceil(min / step) * step;
  const ticks: number[] = [];
  for (let value = start; value <= max + step * 1e-9; value += step) {
    ticks.push(Math.round(value / step) * step);
  }
  return ticks.length ? ticks : [min, max];
}

function tickDecimals(step: number): number {
  return Math.max(0, Math.ceil(-Math.log10(step)));
}

function formatTick(value: number, step: number): string {
  return value.toFixed(tickDecimals(step));
}

function drawSeries(
  context: CanvasRenderingContext2D,
  kind: PlotSpec["kind"],
  series: PlotSpec["series"][number],
  frame: { x: (value: number) => number; y: (value: number) => number },
  palette: PlotPalette,
  highlightRail: string | null,
  hoverPoint: PlotPoint | null,
  selectedId: string | null,
  fadeInherited: boolean,
  densityScale = 1,
): void {
  const color = series.color || palette.signal;
  if (kind === "line") {
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.beginPath();
    series.points.forEach((point, index) => {
      if (index === 0) context.moveTo(frame.x(point.x), frame.y(point.y));
      else context.lineTo(frame.x(point.x), frame.y(point.y));
    });
    context.stroke();
  }
  const radius = markerRadius(kind, series.points.length, densityScale);
  if (radius <= 0) return;
  for (const point of series.points) {
    const px = frame.x(point.x);
    const py = frame.y(point.y);
    const railMate = Boolean(highlightRail && point.rail === highlightRail);
    const hovered = hoverPoint != null && point.id != null && point.id === hoverPoint.id;
    const selected = Boolean(selectedId && point.id === selectedId);
    const faded = fadeInherited && Boolean(point.inherited);
    const size = radius * (hovered || selected ? 1.7 : railMate ? 1.35 : 1);
    context.globalAlpha = point.ignored ? 0.38 : faded ? 0.28 : railMate && !hovered ? 0.78 : 1;
    context.fillStyle = point.color || color;
    context.beginPath();
    context.arc(px, py, size * (faded ? 0.85 : 1), 0, Math.PI * 2);
    if (point.ignored) {
      context.strokeStyle = point.color || color;
      context.lineWidth = 1.4;
      context.stroke();
    } else {
      context.fill();
    }
    if (hovered || selected) {
      context.globalAlpha = 1;
      context.strokeStyle = palette.foreground;
      context.lineWidth = 1.5;
      context.beginPath();
      context.arc(px, py, size + 2.5, 0, Math.PI * 2);
      context.stroke();
    }
  }
  context.globalAlpha = 1;
}

function markerRadius(kind: PlotSpec["kind"], pointCount: number, densityScale = 1): number {
  if (kind !== "line") return 2 * densityScale;
  return pointCount > 120 ? 0 : 3 * densityScale;
}

function pointStats(point: PlotPoint, plot: PlotSpec): PlotStat[] {
  if (point.stats?.length) return point.stats;
  return [
    { label: plot.x_label, value: formatNumber(point.x) },
    { label: plot.y_label, value: formatNumber(point.y) },
  ];
}

function formatNumber(value: number): string {
  if (Math.abs(value) >= 10) return value.toFixed(2);
  if (Math.abs(value) >= 1) return value.toFixed(3);
  return value.toFixed(4);
}
