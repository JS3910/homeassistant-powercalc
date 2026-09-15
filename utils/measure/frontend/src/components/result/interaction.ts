import type { PlotMarker, PlotPoint, PlotSpec } from "../../types";

export interface PlotView {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

export interface PlotYRange {
  minY: number;
  maxY: number;
}

export interface PlotFrame {
  left: number;
  top: number;
  plotWidth: number;
  plotHeight: number;
}

export interface PlotLayout extends PlotFrame {
  width: number;
  height: number;
  view: PlotView;
}

export const HIT_RADIUS_PX = 16;
export const INTEREST_TICK_DEPTH = 14;
/** Inset so edge markers sit inside the clip instead of being cut in half. */
export const PLOT_POINT_PAD = 8;
const MIN_SPAN_RATIO = 0.012;

export function dataBounds(plot: PlotSpec): PlotView {
  const points = plot.series.flatMap((series) => series.points);
  const [minX, maxX] = axisDomain(
    points.map((point) => point.x),
    plot.x_min,
    plot.x_max,
  );
  const [minY, maxY] = extent(points.map((point) => point.y));
  return { minX, maxX, minY, maxY };
}

export function viewsEqual(
  left: PlotView,
  right: PlotView,
  epsilon = 1e-9,
): boolean {
  return (
    Math.abs(left.minX - right.minX) <= epsilon &&
    Math.abs(left.maxX - right.maxX) <= epsilon &&
    yViewsEqual(left, right, epsilon)
  );
}

export function yViewsEqual(
  left: PlotYRange,
  right: PlotYRange,
  epsilon = 1e-9,
): boolean {
  return (
    Math.abs(left.minY - right.minY) <= epsilon &&
    Math.abs(left.maxY - right.maxY) <= epsilon
  );
}

/** Keep the previous object when the range did not move, so Lit does not redraw. */
export function reuseYRange(
  previous: PlotYRange | null | undefined,
  next: PlotYRange | null,
): PlotYRange | null {
  if (previous && next && yViewsEqual(previous, next)) return previous;
  return next;
}

/** Copy a power-axis window onto another plot without moving its X. */
export function applyYToView(view: PlotView, y: PlotYRange): PlotView {
  return { ...view, minY: y.minY, maxY: y.maxY };
}

export function plotMargins(_plot: PlotSpec): {
  left: number;
  right: number;
  top: number;
  bottom: number;
} {
  return { left: 68, right: 24, top: 22, bottom: 58 };
}

export function plotLayout(
  plot: PlotSpec,
  width: number,
  height: number,
  view?: PlotView | null,
): PlotLayout {
  const margins = plotMargins(plot);
  const frame: PlotFrame = {
    left: margins.left,
    top: margins.top,
    plotWidth: Math.max(1, width - margins.left - margins.right),
    plotHeight: Math.max(1, height - margins.top - margins.bottom),
  };
  return { ...frame, width, height, view: view ?? dataBounds(plot) };
}

export function plotInnerFrame(layout: PlotFrame): PlotFrame {
  const pad = PLOT_POINT_PAD;
  return {
    left: layout.left + pad,
    top: layout.top + pad,
    plotWidth: Math.max(1, layout.plotWidth - 2 * pad),
    plotHeight: Math.max(1, layout.plotHeight - 2 * pad),
  };
}

export function dataToPixel(
  layout: PlotLayout,
  x: number,
  y: number,
): { x: number; y: number } {
  const { view } = layout;
  const inner = plotInnerFrame(layout);
  const spanX = view.maxX - view.minX || 1;
  const spanY = view.maxY - view.minY || 1;
  return {
    x: inner.left + ((x - view.minX) / spanX) * inner.plotWidth,
    y: inner.top + inner.plotHeight - ((y - view.minY) / spanY) * inner.plotHeight,
  };
}

export function pixelToData(
  layout: PlotLayout,
  x: number,
  y: number,
): { x: number; y: number } {
  const { view } = layout;
  const inner = plotInnerFrame(layout);
  const spanX = view.maxX - view.minX || 1;
  const spanY = view.maxY - view.minY || 1;
  return {
    x: view.minX + ((x - inner.left) / inner.plotWidth) * spanX,
    y: view.minY + ((inner.top + inner.plotHeight - y) / inner.plotHeight) * spanY,
  };
}

export function inPlot(layout: PlotLayout, x: number, y: number): boolean {
  return inPlotX(layout, x) && y >= layout.top && y <= layout.top + layout.plotHeight;
}

export function inPlotX(layout: PlotLayout, x: number): boolean {
  return x >= layout.left && x <= layout.left + layout.plotWidth;
}

export function plotInterestMarks(plot: PlotSpec): PlotMarker[] {
  if (plot.markers?.length) return plot.markers;
  const seen = new Map<number, string>();
  for (const point of plotPoints(plot)) {
    if (!point.interest) continue;
    if (!seen.has(point.x)) seen.set(point.x, point.interest);
  }
  return [...seen].map(([x, label]) => ({ x, label }));
}

export function interestTickPixel(
  layout: PlotLayout,
  markX: number,
  edge: "top" | "bottom" = "bottom",
): { x: number; y: number } {
  return {
    x: dataToPixel(layout, markX, layout.view.minY).x,
    y: edge === "top" ? layout.top : layout.top + layout.plotHeight,
  };
}

/** Hit-test the gold interest triangles above and below the plot. */
export function interestTickHit(
  layout: PlotLayout,
  marks: readonly PlotMarker[],
  pixelX: number,
  pixelY: number,
  maxDistance = HIT_RADIUS_PX,
): PlotMarker | null {
  const topY = layout.top;
  const axisY = layout.top + layout.plotHeight;
  const inBottom = pixelY >= axisY - 2 && pixelY <= axisY + INTEREST_TICK_DEPTH;
  const inTop = pixelY >= topY - INTEREST_TICK_DEPTH && pixelY <= topY + 2;
  if (!inBottom && !inTop) return null;
  let best: PlotMarker | null = null;
  let bestDistance = maxDistance * maxDistance;
  for (const mark of marks) {
    if (mark.x < layout.view.minX || mark.x > layout.view.maxX) continue;
    const x = interestTickPixel(layout, mark.x).x;
    const distance = (x - pixelX) ** 2;
    if (distance <= bestDistance) {
      best = mark;
      bestDistance = distance;
    }
  }
  return best;
}

/** Hue plots mark a whole rail; arrows are enough, don't ring every sample. */
export function interestCirclePoints(plot: PlotSpec): PlotPoint[] {
  if (plot.id === "hs" || plot.id === "hs_max_bri" || plot.id === "hs_cylinder") return [];
  const points: PlotPoint[] = [];
  for (const mark of plotInterestMarks(plot)) {
    const point = pointForInterestMark(plot, mark);
    if (point) points.push(point);
  }
  return points;
}

export function pointForInterestMark(
  plot: PlotSpec,
  mark: PlotMarker,
): PlotPoint | null {
  const matches = plotPoints(plot).filter(
    (point) =>
      point.interest === mark.label ||
      Math.abs(point.x - mark.x) <= 1e-6,
  );
  if (!matches.length) return null;
  return matches.reduce((best, point) => (point.y > best.y ? point : best));
}

export function hoverPointForInterestMark(
  plot: PlotSpec,
  mark: PlotMarker,
): PlotPoint {
  const existing = pointForInterestMark(plot, mark);
  if (existing) {
    return existing.interest ? existing : { ...existing, interest: mark.label };
  }
  return {
    x: mark.x,
    y: 0,
    color: null,
    interest: mark.label,
    stats: [{ label: plot.x_label, value: `${Math.round(mark.x)}` }],
  };
}

export interface HistogramBin {
  x0: number;
  x1: number;
  count: number;
  /** Data-x with the most samples in this bin; used so hover links the pile, not a neighbor. */
  anchor: number;
}

/** One exact sample-x and how many measurements sit there. */
export interface HistogramPile {
  x: number;
  count: number;
}

/** Pixel radius for snapping the histogram cursor to a visible bar. */
export const HISTOGRAM_SNAP_PX = 8;
/** Comfortable spacing: keep the default marker size. */
const DENSITY_NN_FULL_PX = 8;
/** Piled-on-top spacing: shrink markers to 75% of full size. */
const DENSITY_NN_MIN_PX = 2.5;
const DENSITY_SCALE_MIN = 0.75;

/** Sibling whose points should be counted on this plot's x-axis, if any. */
export function densityPlotFor(
  plots: readonly PlotSpec[],
  plot: PlotSpec,
): PlotSpec | null {
  if (plot.id === "hs_max_bri") return plots.find((item) => item.id === "hs") ?? null;
  return null;
}

export function densityX(target: PlotSpec, point: PlotPoint): number | null {
  if (target.id === "hs_max_bri") {
    const hue = hsHueFromPointId(point.id);
    return hue == null ? null : (hue / 65535) * 360;
  }
  if (target.id === "color_temp_max_bri") {
    const mired = ctMiredFromPointId(point.id);
    return mired == null ? point.x : 1_000_000 / mired;
  }
  return point.x;
}

export function densityValues(target: PlotSpec, source: PlotSpec): number[] {
  return plotPoints(source)
    .map((point) => densityX(target, point))
    .filter((value): value is number => value != null);
}

export function histogramBins(
  values: readonly number[],
  minX: number,
  maxX: number,
  binCount: number,
): HistogramBin[] {
  const count = Math.max(1, Math.round(binCount));
  const span = maxX - minX || 1;
  const bins: HistogramBin[] = Array.from({ length: count }, (_, index) => ({
    x0: minX + (span * index) / count,
    x1: minX + (span * (index + 1)) / count,
    count: 0,
    anchor: minX + (span * (index + 0.5)) / count,
  }));
  const freqs = bins.map(() => new Map<number, number>());
  for (const value of values) {
    if (value < minX || value > maxX) continue;
    const index = Math.min(count - 1, Math.max(0, Math.floor(((value - minX) / span) * count)));
    bins[index]!.count += 1;
    const freq = freqs[index]!;
    freq.set(value, (freq.get(value) ?? 0) + 1);
  }
  for (const [index, bin] of bins.entries()) {
    bin.anchor = histogramMode(freqs[index]!, bin.anchor);
  }
  return bins;
}

function histogramMode(freq: Map<number, number>, fallback: number): number {
  let best = fallback;
  let bestCount = 0;
  for (const [value, count] of freq) {
    if (count > bestCount) {
      best = value;
      bestCount = count;
    }
  }
  return best;
}

export function histogramBinAt(
  bins: readonly HistogramBin[],
  x: number,
): HistogramBin | null {
  if (!bins.length) return null;
  const minX = bins[0]!.x0;
  const maxX = bins[bins.length - 1]!.x1;
  if (x < minX || x > maxX) return null;
  const span = maxX - minX || 1;
  const index = Math.min(
    bins.length - 1,
    Math.max(0, Math.floor(((x - minX) / span) * bins.length)),
  );
  return bins[index] ?? null;
}

export function histogramCountAt(
  bins: readonly HistogramBin[],
  x: number,
): number {
  return histogramBinAt(bins, x)?.count ?? 0;
}

export function histogramAnchorAt(
  bins: readonly HistogramBin[],
  x: number,
): number | null {
  const bin = histogramBinAt(bins, x);
  if (!bin?.count) return null;
  return bin.anchor;
}

export function histogramPiles(values: readonly number[]): HistogramPile[] {
  const freq = new Map<number, number>();
  for (const value of values) {
    freq.set(value, (freq.get(value) ?? 0) + 1);
  }
  return [...freq.entries()]
    .map(([x, count]) => ({ x, count }))
    .sort((left, right) => left.x - right.x);
}

export function pileColumn(
  x: number,
  minX: number,
  maxX: number,
  plotWidth: number,
): number {
  const span = maxX - minX || 1;
  return Math.round(((x - minX) / span) * plotWidth);
}

/** Visible bars at this zoom: one per pixel column, the taller pile wins. */
export function visibleHistogramPiles(
  piles: readonly HistogramPile[],
  minX: number,
  maxX: number,
  plotWidth: number,
): HistogramPile[] {
  const byColumn = new Map<number, HistogramPile>();
  for (const pile of piles) {
    if (pile.x < minX || pile.x > maxX) continue;
    const column = pileColumn(pile.x, minX, maxX, plotWidth);
    const existing = byColumn.get(column);
    if (!existing || pile.count > existing.count) byColumn.set(column, pile);
  }
  return [...byColumn.entries()]
    .sort((left, right) => left[0] - right[0])
    .map(([, pile]) => pile);
}

/**
 * Snap to a visible bar. Nearby taller bars beat a short bar the cursor is
 * sitting on — those short ones are what the eye treats as noise when zoomed out.
 */
export function snapHistogramPile(
  piles: readonly HistogramPile[],
  x: number,
  minX: number,
  maxX: number,
  plotWidth: number,
  snapPx = HISTOGRAM_SNAP_PX,
): HistogramPile | null {
  const visible = visibleHistogramPiles(piles, minX, maxX, plotWidth);
  if (!visible.length) return null;
  const span = maxX - minX || 1;
  const cursorPx = ((x - minX) / span) * plotWidth;
  let best: HistogramPile | null = null;
  let bestCount = -1;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const pile of visible) {
    const pilePx = ((pile.x - minX) / span) * plotWidth;
    const dist = Math.abs(pilePx - cursorPx);
    if (dist > snapPx) continue;
    if (pile.count > bestCount || (pile.count === bestCount && dist < bestDist)) {
      best = pile;
      bestCount = pile.count;
      bestDist = dist;
    }
  }
  return best;
}

/** Noun for “N samples at this …”, lowercase and without a unit. */
export function samplesAtNoun(plot: Pick<PlotSpec, "id" | "x_label">): string {
  if (plot.id === "color_temp_max_bri") return "color temperature";
  if (plot.id === "hs_max_bri") return "hue";
  if (
    plot.id === "brightness" ||
    plot.id === "color_temp" ||
    plot.id === "hs" ||
    plot.id === "effect"
  ) {
    return "brightness";
  }
  return plot.x_label.replace(/\s*\([^)]*\)\s*$/, "").trim().toLowerCase() || "value";
}

export function histogramCountForX(
  piles: readonly HistogramPile[],
  x: number,
): number {
  let best = 0;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const pile of piles) {
    const dist = Math.abs(pile.x - x);
    if (dist < bestDist) {
      best = pile.count;
      bestDist = dist;
    }
  }
  return bestDist <= 1e-9 ? best : 0;
}

/** Median nearest-neighbor distance in pixel space. */
export function medianNearestNeighborPx(
  points: readonly { x: number; y: number }[],
): number {
  if (points.length < 2) return Number.POSITIVE_INFINITY;
  const sorted = [...points].sort((left, right) => left.x - right.x);
  const distances: number[] = [];
  for (let index = 0; index < sorted.length; index += 1) {
    const point = sorted[index]!;
    let best = Number.POSITIVE_INFINITY;
    for (let other = index - 1; other >= 0; other -= 1) {
      const dx = point.x - sorted[other]!.x;
      if (dx >= best) break;
      best = Math.min(best, Math.hypot(dx, point.y - sorted[other]!.y));
    }
    for (let other = index + 1; other < sorted.length; other += 1) {
      const dx = sorted[other]!.x - point.x;
      if (dx >= best) break;
      best = Math.min(best, Math.hypot(dx, sorted[other]!.y - point.y));
    }
    distances.push(best);
  }
  distances.sort((left, right) => left - right);
  return distances[Math.floor(distances.length / 2)]!;
}

/**
 * Shrink markers toward 75% when visible points pile on top of each other.
 * A sparse rail (median neighbor ≥ 8px) stays at full size.
 */
export function markerDensityScale(
  points: readonly { x: number; y: number }[],
): number {
  const nearest = medianNearestNeighborPx(points);
  if (!Number.isFinite(nearest)) return 1;
  const t = (nearest - DENSITY_NN_MIN_PX) / (DENSITY_NN_FULL_PX - DENSITY_NN_MIN_PX);
  return Math.min(1, Math.max(DENSITY_SCALE_MIN, DENSITY_SCALE_MIN + (1 - DENSITY_SCALE_MIN) * t));
}

export function nearestPointByX(
  points: readonly PlotPoint[],
  x: number,
): PlotPoint | null {
  let best: PlotPoint | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const point of points) {
    const distance = Math.abs(point.x - x);
    const closer = distance < bestDistance - 1e-9;
    const sameAndTaller =
      Math.abs(distance - bestDistance) <= 1e-9 && point.y > (best?.y ?? Number.NEGATIVE_INFINITY);
    if (closer || sameAndTaller) {
      best = point;
      bestDistance = distance;
    }
  }
  return best;
}

export function panView(
  view: PlotView,
  deltaX: number,
  deltaY: number,
): PlotView {
  return {
    minX: view.minX - deltaX,
    maxX: view.maxX - deltaX,
    minY: view.minY - deltaY,
    maxY: view.maxY - deltaY,
  };
}

export function zoomView(
  view: PlotView,
  factor: number,
  originX: number,
  originY: number,
): PlotView {
  const scale = 1 / factor;
  return {
    minX: originX - (originX - view.minX) * scale,
    maxX: originX + (view.maxX - originX) * scale,
    minY: originY - (originY - view.minY) * scale,
    maxY: originY + (view.maxY - originY) * scale,
  };
}

export function clampView(view: PlotView, bounds: PlotView): PlotView {
  const next = {
    minX: clampAxis(view.minX, view.maxX, bounds.minX, bounds.maxX),
    minY: clampAxis(view.minY, view.maxY, bounds.minY, bounds.maxY),
  };
  return {
    minX: next.minX[0],
    maxX: next.minX[1],
    minY: next.minY[0],
    maxY: next.minY[1],
  };
}

/** Crosshair follows the snapped sample when one is in range; otherwise the cursor. */
export function crosshairData(
  hover: { x: number; y: number } | null,
  hoverPoint?: PlotPoint | null,
): { x: number; y: number } | null {
  if (hoverPoint) return { x: hoverPoint.x, y: hoverPoint.y };
  return hover;
}

export function nearestPoint(
  points: readonly PlotPoint[],
  layout: PlotLayout,
  pixelX: number,
  pixelY: number,
  maxDistance = HIT_RADIUS_PX,
): PlotPoint | null {
  let best: PlotPoint | null = null;
  let bestDistance = maxDistance * maxDistance;
  for (const point of points) {
    const pixel = dataToPixel(layout, point.x, point.y);
    const distance = (pixel.x - pixelX) ** 2 + (pixel.y - pixelY) ** 2;
    if (distance <= bestDistance) {
      best = point;
      bestDistance = distance;
    }
  }
  return best;
}

export function plotPoints(plot: PlotSpec): PlotPoint[] {
  return plot.series.flatMap((series) => series.points);
}

/** Hide seed points copied from an earlier session. */
export function filterPlotToSession(
  plot: PlotSpec,
  sessionOnly: boolean,
): PlotSpec {
  if (!sessionOnly) return plot;
  const series = plot.series
    .map((item) => ({
      ...item,
      points: item.points.filter((point) => !point.inherited),
    }))
    .filter((item) => item.points.length);
  return { ...plot, series };
}

const PLOT_PAIRS: readonly (readonly [string, string])[] = [
  ["hs", "hs_max_bri"],
  ["color_temp", "color_temp_max_bri"],
];

export type PlotPair = "hs" | "color_temp";

/** Shared power axis for a linked pair (HS brightness + hue rail, or CT + kelvin rail). */
export function sharedYForPlot(
  plots: readonly PlotSpec[],
  plotId: string,
): { minY: number; maxY: number } | null {
  const pair = PLOT_PAIRS.find((ids) => ids.includes(plotId));
  if (!pair) return null;
  const members = plots.filter((plot) => pair.includes(plot.id));
  if (members.length < 2) return null;
  const [minY, maxY] = extent(members.flatMap((plot) => plotPoints(plot).map((point) => point.y)));
  return niceYDomain(minY, maxY);
}

/** Expand a power range so paired plots share the same tick edges (usually 0 … nice max). */
export function niceYDomain(minY: number, maxY: number): PlotYRange {
  if (!Number.isFinite(minY) || !Number.isFinite(maxY)) return { minY: 0, maxY: 1 };
  let min = Math.min(minY, maxY);
  let max = Math.max(minY, maxY);
  if (max === min) {
    const padding = Math.max(1, Math.abs(min) * 0.1);
    min -= padding;
    max += padding;
  }
  if (min >= 0) min = 0;
  const step = niceAxisStep(max - min, 5);
  const niceMin = Math.floor(min / step) * step;
  const niceMax = Math.ceil(max / step) * step;
  return { minY: niceMin, maxY: niceMax > niceMin ? niceMax : niceMin + step };
}

function niceAxisStep(range: number, targetCount: number): number {
  if (!(range > 0)) return 1;
  const roughStep = range / targetCount;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const residual = roughStep / magnitude;
  const niceResidual = residual >= 5 ? 10 : residual >= 2 ? 5 : residual >= 1 ? 2 : 1;
  return niceResidual * magnitude;
}

export function plotPairOf(plotId: string): PlotPair | null {
  if (plotId === "hs" || plotId === "hs_max_bri") return "hs";
  if (plotId === "color_temp" || plotId === "color_temp_max_bri") return "color_temp";
  return null;
}

/** Integer brightness from an HS point id `hs:{bri}:{hue}:{sat}`. */
export function hsBriFromPointId(pointId?: string | null): number | null {
  if (!pointId) return null;
  const parts = pointId.split(":");
  if (parts[0] !== "hs" || parts.length < 4) return null;
  const bri = Number(parts[1]);
  return Number.isInteger(bri) ? bri : null;
}

/** Integer hue from an HS point id `hs:{bri}:{hue}:{sat}`. */
export function hsHueFromPointId(pointId?: string | null): number | null {
  if (!pointId) return null;
  const parts = pointId.split(":");
  if (parts[0] !== "hs" || parts.length < 4) return null;
  const hue = Number(parts[2]);
  return Number.isInteger(hue) ? hue : null;
}

/** Integer saturation from an HS point id `hs:{bri}:{hue}:{sat}`. */
export function hsSatFromPointId(pointId?: string | null): number | null {
  if (!pointId) return null;
  const parts = pointId.split(":");
  if (parts[0] !== "hs" || parts.length < 4) return null;
  const sat = Number(parts[3]);
  return Number.isInteger(sat) ? sat : null;
}

export type IsoLineKey = "mired" | "hue" | "sat";

export function isoValue(point: PlotPoint, key: IsoLineKey): number | null {
  if (key === "mired") return ctMiredFromPointId(point.id);
  if (key === "hue") return hsHueFromPointId(point.id);
  return hsSatFromPointId(point.id);
}

/** Groups of 2+ points that share a third-axis value, ordered along X. */
export function isoGroups(
  points: readonly PlotPoint[],
  key: IsoLineKey,
): PlotPoint[][] {
  const groups = new Map<number, PlotPoint[]>();
  for (const point of points) {
    const value = isoValue(point, key);
    if (value == null) continue;
    const group = groups.get(value);
    if (group) group.push(point);
    else groups.set(value, [point]);
  }
  return [...groups.values()]
    .map((group) => [...group].sort((left, right) => left.x - right.x || left.y - right.y))
    .filter((group) => group.length >= 2);
}

export function holdLinksFromPointId(pointId: string): {
  hue: number | null;
  bri: number | null;
  mired: number | null;
} {
  return {
    hue: hsHueFromPointId(pointId),
    bri: ctBriFromPointId(pointId) ?? hsBriFromPointId(pointId),
    mired: ctMiredFromPointId(pointId),
  };
}

/** Same point, or the paired plot's point on the same hue / mired. */
export function matePointId(plot: PlotSpec, pointId: string): string | null {
  if (findPoint(plot, pointId)) return pointId;
  const hue = hsHueFromPointId(pointId);
  if (hue != null) {
    const matches = plotPoints(plot).filter((point) => hsHueFromPointId(point.id) === hue);
    if (!matches.length) return null;
    const sat = Number(pointId.split(":")[3]);
    const sameSat = matches.filter((point) => Number(point.id?.split(":")[3]) === sat);
    const pool = sameSat.length ? sameSat : matches;
    return (
      pool.reduce((best, point) =>
        (hsBriFromPointId(point.id) ?? 0) > (hsBriFromPointId(best.id) ?? 0) ? point : best,
      ).id ?? null
    );
  }
  const mired = ctMiredFromPointId(pointId);
  if (mired != null) {
    const matches = plotPoints(plot).filter((point) => ctMiredFromPointId(point.id) === mired);
    if (!matches.length) return null;
    return (
      matches.reduce((best, point) =>
        (ctBriFromPointId(point.id) ?? 0) > (ctBriFromPointId(best.id) ?? 0) ? point : best,
      ).id ?? null
    );
  }
  return null;
}

/** Closest measured hue to a hue-axis position, wrapping at 360°. */
export function nearestHueAt(
  points: readonly PlotPoint[],
  hueDegrees: number,
): number | null {
  let best: number | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const point of points) {
    const hue = hsHueFromPointId(point.id);
    if (hue == null) continue;
    const degrees = (hue / 65535) * 360;
    const raw = Math.abs(degrees - hueDegrees) % 360;
    const distance = Math.min(raw, 360 - raw);
    if (distance < bestDistance) {
      best = hue;
      bestDistance = distance;
    }
  }
  return best;
}

/** Restrict the HS brightness plot to one hue, or return the plot unchanged. */
export function filterPlotToHue(plot: PlotSpec, hue: number | null): PlotSpec {
  if (hue == null || plot.id !== "hs") return plot;
  const series = plot.series
    .map((item) => ({
      ...item,
      points: item.points.filter((point) => hsHueFromPointId(point.id) === hue),
    }))
    .filter((item) => item.points.length);
  if (!series.length) return plot;
  const degrees = Math.round((hue / 65535) * 360);
  return { ...plot, title: `Hue and saturation · ${degrees}°`, series };
}

/** Integer brightness from a CT point id `color_temp:{bri}:{mired}`. */
export function ctBriFromPointId(pointId?: string | null): number | null {
  if (!pointId) return null;
  const parts = pointId.split(":");
  if (parts[0] !== "color_temp" || parts.length < 3) return null;
  const bri = Number(parts[1]);
  return Number.isInteger(bri) ? bri : null;
}

/** Integer mired from a CT point id `color_temp:{bri}:{mired}`. */
export function ctMiredFromPointId(pointId?: string | null): number | null {
  if (!pointId) return null;
  const parts = pointId.split(":");
  if (parts[0] !== "color_temp" || parts.length < 3) return null;
  const mired = Number(parts[2]);
  return Number.isInteger(mired) && mired > 0 ? mired : null;
}

/** Closest measured mired to a kelvin-axis position. */
export function nearestMiredAt(
  points: readonly PlotPoint[],
  kelvin: number,
): number | null {
  if (!(kelvin > 0)) return null;
  let best: number | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const point of points) {
    const mired = ctMiredFromPointId(point.id);
    if (mired == null) continue;
    const distance = Math.abs(1_000_000 / mired - kelvin);
    if (distance < bestDistance) {
      best = mired;
      bestDistance = distance;
    }
  }
  return best;
}

/** Restrict the CT brightness plot to one mired, or return the plot unchanged. */
export function filterPlotToMired(
  plot: PlotSpec,
  mired: number | null,
): PlotSpec {
  if (mired == null || plot.id !== "color_temp") return plot;
  const series = plot.series
    .map((item) => ({
      ...item,
      points: item.points.filter(
        (point) => ctMiredFromPointId(point.id) === mired,
      ),
    }))
    .filter((item) => item.points.length);
  if (!series.length) return plot;
  return {
    ...plot,
    title: `Color temperature · ${Math.round(1_000_000 / mired)} K`,
    series,
  };
}

/** Closest measured CT brightness to a 0–100 brightness-axis position. */
export function nearestBriAt(
  points: readonly PlotPoint[],
  brightnessPct: number,
): number | null {
  const target = (brightnessPct / 100) * 255;
  let best: number | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const point of points) {
    const bri = ctBriFromPointId(point.id);
    if (bri == null) continue;
    const distance = Math.abs(bri - target);
    if (distance < bestDistance) {
      best = bri;
      bestDistance = distance;
    }
  }
  return best;
}

function pointBrightness(point: PlotPoint): number | null {
  return ctBriFromPointId(point.id) ?? hsBriFromPointId(point.id);
}

/** Measured brightness values in the plot, low to high. */
export function brightnessLevels(plot: PlotSpec): number[] {
  const levels = new Set<number>();
  for (const point of plotPoints(plot)) {
    const bri = pointBrightness(point);
    if (bri != null) levels.add(bri);
  }
  return [...levels].sort((left, right) => left - right);
}

function maxBriInPlot(plot: PlotSpec): number | null {
  const levels = brightnessLevels(plot);
  return levels.length ? levels[levels.length - 1]! : null;
}

/** Restrict the CT kelvin rail to one brightness. Null is idle: 100% only. */
export function filterPlotToBrightness(
  plot: PlotSpec,
  bri: number | null,
): PlotSpec {
  if (plot.id !== "color_temp_max_bri") return plot;
  const maxBri = maxBriInPlot(plot);
  const selected = bri ?? maxBri;
  if (selected == null) return plot;
  const series = slicePlotToBri(plot, selected);
  if (!series.length) {
    if (bri == null || maxBri == null || selected === maxBri) return plot;
    return filterPlotToBrightness(plot, null);
  }
  const title =
    bri == null
      ? "Color temperature at 100%"
      : `Color temperature at ${Math.round((selected / 255) * 100)}%`;
  return { ...plot, title, series };
}

/** Restrict the HS cylinder to one brightness. Null is idle: 100% only. */
export function filterPlotToHsBrightness(
  plot: PlotSpec,
  bri: number | null,
): PlotSpec {
  if (plot.id !== "hs_cylinder") return plot;
  const maxBri = maxBriInPlot(plot);
  const selected = bri ?? maxBri;
  if (selected == null) return plot;
  const series = slicePlotToBri(plot, selected);
  if (!series.length) {
    if (bri == null || maxBri == null || selected === maxBri) return plot;
    return filterPlotToHsBrightness(plot, null);
  }
  const title =
    bri == null
      ? "Hue / saturation at 100%"
      : `Hue / saturation at ${Math.round((selected / 255) * 100)}%`;
  return { ...plot, title, series };
}

function slicePlotToBri(plot: PlotSpec, bri: number): PlotSpec["series"] {
  return plot.series
    .map((item) => ({
      ...item,
      points: item.points.filter((point) => pointBrightness(point) === bri),
    }))
    .filter((item) => item.points.length);
}

export function findPoint(plot: PlotSpec, pointId: string): PlotPoint | null {
  return plotPoints(plot).find((point) => point.id === pointId) ?? null;
}

export function pointerDistance(
  a: { x: number; y: number },
  b: { x: number; y: number },
): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

export function midpoint(
  a: { x: number; y: number },
  b: { x: number; y: number },
): { x: number; y: number } {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export function wheelZoomFactor(deltaY: number): number {
  return deltaY < 0 ? 1.15 : 1 / 1.15;
}

export function axisDomain(
  values: number[],
  configuredMin?: number | null,
  configuredMax?: number | null,
): [number, number] {
  const [dataMin, dataMax] = extent(values);
  const minimum =
    configuredMin == null ? dataMin : Math.min(configuredMin, dataMin);
  const maximum =
    configuredMax == null ? dataMax : Math.max(configuredMax, dataMax);
  return minimum === maximum ? extent([minimum]) : [minimum, maximum];
}

export function extent(values: number[]): [number, number] {
  let minimum = Number.POSITIVE_INFINITY;
  let maximum = Number.NEGATIVE_INFINITY;
  for (const value of values) {
    minimum = Math.min(minimum, value);
    maximum = Math.max(maximum, value);
  }
  if (minimum !== maximum) return [minimum, maximum];
  const padding = Math.max(1, Math.abs(minimum) * 0.1);
  return [minimum - padding, maximum + padding];
}

function clampAxis(
  min: number,
  max: number,
  boundMin: number,
  boundMax: number,
): [number, number] {
  const boundSpan = Math.max(boundMax - boundMin, 1e-9);
  const minSpan = boundSpan * MIN_SPAN_RATIO;
  const span = Math.max(minSpan, max - min);
  if (span >= boundSpan) return [boundMin, boundMax];
  let nextMin = min;
  let nextMax = min + span;
  if (nextMin < boundMin) {
    nextMin = boundMin;
    nextMax = nextMin + span;
  }
  if (nextMax > boundMax) {
    nextMax = boundMax;
    nextMin = nextMax - span;
  }
  return [nextMin, nextMax];
}
