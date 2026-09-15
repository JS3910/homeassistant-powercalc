import { HIT_RADIUS_PX, plotPoints } from "./interaction";
import type { PlotPoint, PlotSpec } from "../../types";

/** True isometric elevation from the horizontal: atan(1/√2) ≈ 35.264°. */
export const ISO_ELEVATION = Math.atan(1 / Math.SQRT2);
/** Extra azimuth so yaw 0 is the isometric diagonal. */
const ISO_AZIMUTH = Math.PI / 4;
export const INITIAL_YAW = Math.PI / 6;
export const INITIAL_TILT = ISO_ELEVATION;
export const INITIAL_ZOOM = 1;
export const MIN_TILT = 0;
export const MAX_TILT = (89 * Math.PI) / 180;
export const MIN_ZOOM = 0.45;
export const MAX_ZOOM = 3.5;
const POINT_RADIUS = 1.3;
const AIM = { x: 0, y: 0, z: 0.5 };

export interface CylinderCamera {
  yaw: number;
  tilt: number;
  zoom: number;
}

export interface CylinderDomains {
  satMax: number;
  wattMin: number;
  wattMax: number;
}

export interface CylinderFrame {
  left: number;
  top: number;
  plotWidth: number;
  plotHeight: number;
}

export interface CylinderLayout extends CylinderDomains, CylinderCamera {
  width: number;
  height: number;
  frame: CylinderFrame;
  cx: number;
  cy: number;
  fit: number;
}

export interface CylinderPalette {
  background: string;
  foreground: string;
  muted: string;
  grid: string;
  signal: string;
  warning: string;
}

export interface CylinderOverlay extends CylinderCamera {
  hoverPoint?: PlotPoint | null;
  selectedId?: string | null;
  fadeInherited?: boolean;
  /** Full-set points so a brightness slice keeps a stable power scale. */
  domainPoints?: readonly PlotPoint[];
}

export function defaultCamera(): CylinderCamera {
  return { yaw: INITIAL_YAW, tilt: INITIAL_TILT, zoom: INITIAL_ZOOM };
}

export function pointWatt(point: PlotPoint): number {
  return point.z ?? point.y;
}

export function yawClose(left: number, right: number, epsilon = 1e-6): boolean {
  const delta = Math.atan2(Math.sin(left - right), Math.cos(left - right));
  return Math.abs(delta) <= epsilon;
}

export function cameraAtRest(camera: CylinderCamera): boolean {
  return (
    yawClose(camera.yaw, INITIAL_YAW) &&
    Math.abs(camera.tilt - INITIAL_TILT) <= 1e-6 &&
    Math.abs(camera.zoom - INITIAL_ZOOM) <= 1e-6
  );
}

/** Drag right turns the cylinder clockwise as seen from above. */
export function yawFromDrag(yaw: number, dx: number, width: number): number {
  return yaw - (dx / Math.max(width, 1)) * Math.PI;
}

/** Drag down tips the top toward you (camera climbs). Horizon is elevation 0. */
export function tiltFromDrag(tilt: number, dy: number, height: number): number {
  return clampTilt(tilt + (dy / Math.max(height, 1)) * (Math.PI / 2));
}

export function clampTilt(tilt: number): number {
  return Math.min(MAX_TILT, Math.max(MIN_TILT, tilt));
}

export function clampZoom(zoom: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

/** More power-axis ticks as the projected Z span grows (zoom / tilt). */
export function cylinderPowerTickCount(layout: CylinderLayout): number {
  const top = worldToPixel(layout, { x: 0, y: 0, z: 1 });
  const bottom = worldToPixel(layout, { x: 0, y: 0, z: 0 });
  const span = Math.abs(top.y - bottom.y);
  return Math.max(3, Math.min(16, Math.round(span / 28)));
}

export function zoomFromWheel(zoom: number, deltaY: number): number {
  return clampZoom(zoom * (deltaY < 0 ? 1.12 : 1 / 1.12));
}

export function cylinderDomains(points: readonly PlotPoint[]): CylinderDomains {
  let satMax = 0;
  let wattMin = Number.POSITIVE_INFINITY;
  let wattMax = Number.NEGATIVE_INFINITY;
  for (const point of points) {
    satMax = Math.max(satMax, point.y);
    const watt = pointWatt(point);
    wattMin = Math.min(wattMin, watt);
    wattMax = Math.max(wattMax, watt);
  }
  if (!(satMax > 0)) satMax = 255;
  if (!Number.isFinite(wattMin)) return { satMax, wattMin: 0, wattMax: 1 };
  if (wattMin >= 0) wattMin = 0;
  if (wattMax === wattMin) wattMax = wattMin + 1;
  return { satMax, wattMin, wattMax };
}

/** Hue as angle, saturation as radius, nicened power as height. */
export function cylinderWorld(
  point: PlotPoint,
  domains: CylinderDomains,
): { x: number; y: number; z: number } {
  const radius = domains.satMax > 0 ? point.y / domains.satMax : 0;
  const angle = (point.x * Math.PI) / 180;
  const span = domains.wattMax - domains.wattMin;
  return {
    x: radius * Math.cos(angle),
    y: radius * Math.sin(angle),
    z: span === 0 ? 0 : (pointWatt(point) - domains.wattMin) / span,
  };
}

/**
 * Orbit camera aimed at the cylinder centre. `yaw` 0 + isometric tilt matches
 * the original (1,1,1) view. Larger `depth` is closer to the camera.
 */
export function projectWorld(
  world: { x: number; y: number; z: number },
  yaw = 0,
  tilt = INITIAL_TILT,
): { sx: number; sy: number; depth: number } {
  const azimuth = yaw + ISO_AZIMUTH;
  const { right, up, cam } = cameraBasis(azimuth, tilt);
  const px = world.x - AIM.x;
  const py = world.y - AIM.y;
  const pz = world.z - AIM.z;
  return {
    sx: px * right.x + py * right.y + pz * right.z,
    sy: px * up.x + py * up.y + pz * up.z,
    depth: px * cam.x + py * cam.y + pz * cam.z,
  };
}

function cameraBasis(azimuth: number, elevation: number): {
  right: { x: number; y: number; z: number };
  up: { x: number; y: number; z: number };
  cam: { x: number; y: number; z: number };
} {
  const cosE = Math.cos(elevation);
  const sinE = Math.sin(elevation);
  const cosA = Math.cos(azimuth);
  const sinA = Math.sin(azimuth);
  const cam = { x: cosA * cosE, y: sinA * cosE, z: sinE };
  const forward = { x: -cam.x, y: -cam.y, z: -cam.z };
  let right = {
    x: forward.y * 1 - forward.z * 0,
    y: forward.z * 0 - forward.x * 1,
    z: forward.x * 0 - forward.y * 0,
  };
  const length = Math.hypot(right.x, right.y, right.z);
  if (length < 1e-8) {
    right = { x: -sinA, y: cosA, z: 0 };
  } else {
    right = { x: right.x / length, y: right.y / length, z: right.z / length };
  }
  const up = {
    x: right.y * forward.z - right.z * forward.y,
    y: right.z * forward.x - right.x * forward.z,
    z: right.x * forward.y - right.y * forward.x,
  };
  return { right, up, cam };
}

export function layoutCylinder(
  width: number,
  height: number,
  points: readonly PlotPoint[],
  camera: CylinderCamera = defaultCamera(),
): CylinderLayout {
  const frame: CylinderFrame = {
    left: 56,
    top: 18,
    plotWidth: Math.max(1, width - 76),
    plotHeight: Math.max(1, height - 54),
  };
  const domains = cylinderDomains(points);
  const baseFit = Math.min(frame.plotWidth / 2.2, frame.plotHeight / 2.5);
  return {
    width,
    height,
    frame,
    ...domains,
    ...camera,
    cx: frame.left + frame.plotWidth / 2,
    cy: frame.top + frame.plotHeight / 2,
    fit: baseFit * camera.zoom,
  };
}

export function projectedPixel(
  layout: CylinderLayout,
  point: PlotPoint,
): { x: number; y: number; depth: number } {
  return worldToPixel(layout, cylinderWorld(point, layout));
}

export function worldToPixel(
  layout: CylinderLayout,
  world: { x: number; y: number; z: number },
): { x: number; y: number; depth: number } {
  const projected = projectWorld(world, layout.yaw, layout.tilt);
  return {
    x: layout.cx + projected.sx * layout.fit,
    y: layout.cy - projected.sy * layout.fit,
    depth: projected.depth,
  };
}

export function inCylinderFrame(layout: CylinderLayout, pixelX: number, pixelY: number): boolean {
  const { left, top, plotWidth, plotHeight } = layout.frame;
  return pixelX >= left && pixelX <= left + plotWidth && pixelY >= top && pixelY <= top + plotHeight;
}

export function nearestCylinderPoint(
  points: readonly PlotPoint[],
  layout: CylinderLayout,
  pixelX: number,
  pixelY: number,
  maxDistance = HIT_RADIUS_PX,
): PlotPoint | null {
  let best: PlotPoint | null = null;
  let bestDistance = maxDistance * maxDistance;
  for (const point of points) {
    const pixel = projectedPixel(layout, point);
    const distance = (pixel.x - pixelX) ** 2 + (pixel.y - pixelY) ** 2;
    if (distance <= bestDistance) {
      best = point;
      bestDistance = distance;
    }
  }
  return best;
}

export function drawCylinder(
  context: CanvasRenderingContext2D,
  plot: PlotSpec,
  width: number,
  height: number,
  palette: CylinderPalette,
  overlay: CylinderOverlay,
): CylinderLayout {
  const points = plotPoints(plot);
  const layout = layoutCylinder(width, height, overlay.domainPoints ?? points, overlay);

  context.clearRect(0, 0, width, height);
  context.fillStyle = palette.background;
  context.fillRect(0, 0, width, height);
  context.font = '12px ui-monospace, "SFMono-Regular", monospace';
  context.lineWidth = 1;
  context.textBaseline = "middle";

  drawPowerAxis(context, layout, palette);
  drawWireframe(context, layout, palette, plot);
  drawPoints(context, layout, points, palette, overlay);
  drawHueLabels(context, layout, palette);
  return layout;
}

function drawPowerAxis(
  context: CanvasRenderingContext2D,
  layout: CylinderLayout,
  palette: CylinderPalette,
): void {
  const axisX = layout.frame.left + 4;
  const top = worldToPixel(layout, { x: 0, y: 0, z: 1 });
  const bottom = worldToPixel(layout, { x: 0, y: 0, z: 0 });
  context.strokeStyle = palette.grid;
  context.globalAlpha = 0.55;
  context.beginPath();
  context.moveTo(axisX, top.y);
  context.lineTo(axisX, bottom.y);
  context.stroke();
  context.globalAlpha = 1;
  context.fillStyle = palette.muted;
  context.strokeStyle = palette.grid;
  context.textAlign = "left";
  const step = niceStep(layout.wattMax - layout.wattMin, cylinderPowerTickCount(layout));
  const frameTop = layout.frame.top - 6;
  const frameBottom = layout.frame.top + layout.frame.plotHeight + 6;
  let lastLabelY = Number.POSITIVE_INFINITY;
  for (const tick of niceTicks(layout.wattMin, layout.wattMax, step)) {
    const ratio = (tick - layout.wattMin) / (layout.wattMax - layout.wattMin);
    const pixel = worldToPixel(layout, { x: 0, y: 0, z: ratio });
    if (pixel.y < frameTop || pixel.y > frameBottom) continue;
    context.globalAlpha = 0.7;
    context.beginPath();
    context.moveTo(axisX, pixel.y);
    context.lineTo(axisX + 6, pixel.y);
    context.stroke();
    context.globalAlpha = 1;
    if (Math.abs(pixel.y - lastLabelY) < 14) continue;
    context.fillText(formatTick(tick, step), axisX + 8, pixel.y);
    lastLabelY = pixel.y;
  }
  context.save();
  context.translate(16, (top.y + bottom.y) / 2);
  context.rotate(-Math.PI / 2);
  context.textAlign = "center";
  context.fillText("Power (W)", 0, 0);
  context.restore();
}

function drawWireframe(
  context: CanvasRenderingContext2D,
  layout: CylinderLayout,
  palette: CylinderPalette,
  plot: PlotSpec,
): void {
  context.save();
  context.strokeStyle = palette.grid;
  context.globalAlpha = 0.28;
  drawCircle(context, layout, 1, 0);
  drawCircle(context, layout, 1, 1);
  drawCircle(context, layout, 0.5, 0);
  for (let index = 0; index < 6; index += 1) {
    const angle = (index / 6) * Math.PI * 2;
    const x = Math.cos(angle);
    const y = Math.sin(angle);
    const low = worldToPixel(layout, { x, y, z: 0 });
    const high = worldToPixel(layout, { x, y, z: 1 });
    context.beginPath();
    context.moveTo(low.x, low.y);
    context.lineTo(high.x, high.y);
    context.stroke();
  }
  const axisLow = worldToPixel(layout, { x: 0, y: 0, z: 0 });
  const axisHigh = worldToPixel(layout, { x: 0, y: 0, z: 1 });
  context.setLineDash([3, 4]);
  context.beginPath();
  context.moveTo(axisLow.x, axisLow.y);
  context.lineTo(axisHigh.x, axisHigh.y);
  context.stroke();
  context.setLineDash([]);
  context.globalAlpha = 0.4;
  context.strokeStyle = palette.warning;
  for (const mark of plot.markers ?? []) {
    const angle = (mark.x * Math.PI) / 180;
    const rim = worldToPixel(layout, { x: Math.cos(angle), y: Math.sin(angle), z: 0 });
    context.beginPath();
    context.moveTo(axisLow.x, axisLow.y);
    context.lineTo(rim.x, rim.y);
    context.stroke();
  }
  context.restore();
}

function drawCircle(context: CanvasRenderingContext2D, layout: CylinderLayout, radius: number, z: number): void {
  const steps = 64;
  context.beginPath();
  for (let index = 0; index <= steps; index += 1) {
    const angle = (index / steps) * Math.PI * 2;
    const pixel = worldToPixel(layout, {
      x: radius * Math.cos(angle),
      y: radius * Math.sin(angle),
      z,
    });
    if (index === 0) context.moveTo(pixel.x, pixel.y);
    else context.lineTo(pixel.x, pixel.y);
  }
  context.stroke();
}

function drawHueLabels(
  context: CanvasRenderingContext2D,
  layout: CylinderLayout,
  palette: CylinderPalette,
): void {
  context.fillStyle = palette.muted;
  context.textAlign = "center";
  context.textBaseline = "middle";
  for (const hue of [0, 90, 180, 270]) {
    const angle = (hue * Math.PI) / 180;
    const pixel = worldToPixel(layout, {
      x: 1.18 * Math.cos(angle),
      y: 1.18 * Math.sin(angle),
      z: 0,
    });
    context.fillText(`${hue}°`, pixel.x, pixel.y);
  }
}

function drawPoints(
  context: CanvasRenderingContext2D,
  layout: CylinderLayout,
  points: readonly PlotPoint[],
  palette: CylinderPalette,
  overlay: CylinderOverlay,
): void {
  const drawn = points
    .map((point) => ({ point, pixel: projectedPixel(layout, point) }))
    .sort((left, right) => left.pixel.depth - right.pixel.depth);
  for (const { point, pixel } of drawn) {
    const hovered = overlay.hoverPoint != null && point.id != null && point.id === overlay.hoverPoint.id;
    const selected = overlay.selectedId != null && point.id === overlay.selectedId;
    const faded = overlay.fadeInherited === true && point.inherited === true;
    const size = POINT_RADIUS * (hovered || selected ? 1.7 : 1);
    context.globalAlpha = point.ignored ? 0.38 : faded ? 0.28 : 1;
    context.fillStyle = point.color || palette.signal;
    context.beginPath();
    context.arc(pixel.x, pixel.y, size, 0, Math.PI * 2);
    context.fill();
    if (hovered || selected) {
      context.strokeStyle = palette.foreground;
      context.globalAlpha = 1;
      context.beginPath();
      context.arc(pixel.x, pixel.y, size + 1.8, 0, Math.PI * 2);
      context.stroke();
    }
  }
  context.globalAlpha = 1;
}

function niceStep(range: number, targetCount: number): number {
  if (!(range > 0)) return 1;
  const rough = range / targetCount;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const residual = rough / magnitude;
  const nice = residual >= 5 ? 10 : residual >= 2 ? 5 : residual >= 1 ? 2 : 1;
  return nice * magnitude;
}

function niceTicks(min: number, max: number, step: number): number[] {
  const ticks: number[] = [];
  const start = Math.ceil((min - 1e-9) / step) * step;
  for (let tick = start; tick <= max + 1e-9; tick += step) ticks.push(tick);
  return ticks.length ? ticks : [min, max];
}

function formatTick(value: number, step: number): string {
  const decimals = Math.max(0, Math.ceil(-Math.log10(step)));
  return value.toFixed(decimals);
}
