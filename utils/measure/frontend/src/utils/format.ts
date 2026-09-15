/** Presentation helpers shared by the views, so the same value never reads two ways. */

import type { OperatingPoint } from "../types";

/** Same floor conversion Home Assistant and the measure runner use (as of 2026.8). */
export function kelvinToMired(kelvin: number): number {
  return Math.floor(1_000_000 / kelvin);
}

export function miredToKelvin(mired: number): number {
  return Math.floor(1_000_000 / mired);
}

/** Nearest integer-mired Kelvin the runner will actually measure. */
export function snapKelvinToMired(
  kelvin: number,
  miredBounds?: { min: number; max: number },
): { kelvin: number; mired: number } | null {
  if (!(kelvin > 0) || !Number.isFinite(kelvin)) return null;
  let mired = kelvinToMired(kelvin);
  if (miredBounds) {
    mired = Math.min(miredBounds.max, Math.max(miredBounds.min, mired));
  }
  if (!(mired > 0)) return null;
  return { mired, kelvin: miredToKelvin(mired) };
}

/**
 * Snap a Kelvin edit to an integer mired. `floor(1e6 / mired)` sits at the
 * cool edge of that bucket, so a 1 K down-step stays in the same mired and
 * would otherwise snap back. When the new value does not leave the current
 * mired, step one mired in the direction of travel.
 */
export function stepKelvinToMired(
  kelvin: number,
  fromKelvin: number,
  miredBounds?: { min: number; max: number },
): { kelvin: number; mired: number } | null {
  const snapped = snapKelvinToMired(kelvin, miredBounds);
  if (!snapped) return null;
  const previous = snapKelvinToMired(fromKelvin, miredBounds);
  if (!previous || snapped.mired !== previous.mired || kelvin === fromKelvin) {
    return snapped;
  }
  const next = previous.mired + (kelvin > fromKelvin ? -1 : 1);
  return snapKelvinToMired(miredToKelvin(next), miredBounds);
}

/** HA brightness 1–255 as the nearest percent. */
export function brightnessPercent(bri: number): number {
  return Math.round((bri / 255) * 100);
}

/** HA hue 0–65535 as the nearest degree on the colour wheel. */
export function hueDegrees(hue: number): number {
  return Math.round((hue / 65535) * 360);
}

/** A file or storage size, in the largest unit that keeps the number readable. */
export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

/** A snake_case identifier as prose: `awaiting_confirmation` becomes `Awaiting confirmation`. */
export function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

const SESSION_STATE_LABELS: Record<string, string> = {
  cancelled: "Stopped",
  cancelling: "Stopping",
};

/** A persisted session state as a badge: `cancelled` is Stopped, not Cancelled. */
export function sessionStateLabel(state: string): string {
  return SESSION_STATE_LABELS[state] ?? humanize(state);
}

const SESSION_MODE_LABELS: Record<string, string> = {
  brightness: "Brightness",
  color_temp: "Color temperature",
  hs: "Hue & saturation",
  effect: "Effect",
  white: "White",
};

/** LUT / runner mode id as a heading: `color_temp` → `Color temperature`. */
export function sessionModeLabel(mode?: string | null): string {
  if (!mode) return "—";
  return SESSION_MODE_LABELS[mode] ?? humanize(mode);
}

const SMART_STAGES = new Set([
  "Discovering envelope",
  "Covering interior",
  "Throwing darts",
]);

function looksLikeEntityWait(phase: string): boolean {
  return /to report (on|off)\b/i.test(phase) || /\blight\.[a-z0-9_]+/i.test(phase);
}

function percent255(value: number): string {
  return `${Math.round((value / 255) * 100)}%`;
}

function formatLightActivity(point: Extract<OperatingPoint, { type: "light" }>): string | undefined {
  if (!point.on) return "Lights off";
  const parts: string[] = [];
  if (point.hue != null) parts.push(`hue ${hueDegrees(point.hue)}°`);
  if (point.saturation != null) parts.push(`sat ${percent255(point.saturation)}`);
  if (point.color_temp_mired != null && point.color_temp_mired > 0) {
    parts.push(`${miredToKelvin(point.color_temp_mired)} K`);
  }
  if (point.brightness != null) parts.push(`bri ${percent255(point.brightness)}`);
  if (point.effect) parts.push(point.effect);
  return parts.length ? parts.join(", ") : undefined;
}

function formatOperatingActivity(point?: OperatingPoint | null): string | undefined {
  if (!point) return undefined;
  if (point.type === "light") return formatLightActivity(point);
  if (point.type === "speaker") {
    return point.muted ? "Muted" : `Volume ${Math.round(point.volume)}%`;
  }
  if (point.type === "fan") {
    return point.on ? `Fan ${Math.round(point.percentage)}%` : "Fan off";
  }
  return point.charging
    ? `Charging ${Math.round(point.battery_level)}%`
    : `Battery ${Math.round(point.battery_level)}%`;
}

function activityStage(phase?: string): string | undefined {
  if (!phase) return undefined;
  if (SMART_STAGES.has(phase)) return phase;
  if (phase.startsWith("Full-brightness warm-up")) {
    return phase.replace(/: sending command$/, "");
  }
  if (
    /^Measuring standby/i.test(phase) ||
    /^Checking lowest on-load/i.test(phase) ||
    /^Using lowest on-load/i.test(phase) ||
    phase === "Waiting for power to settle"
  ) {
    return phase;
  }
  if (phase.startsWith("Stabilizing light")) return phase;
  if (looksLikeEntityWait(phase)) return undefined;
  if (SESSION_MODE_LABELS[phase]) return SESSION_MODE_LABELS[phase];
  return undefined;
}

/** Live running-view topline: sweep / stage and the current rail, not entity waits. */
export function runningActivityLabel(input: {
  phase?: string;
  mode?: string | null;
  operating_point?: OperatingPoint | null;
}): string {
  const point = formatOperatingActivity(input.operating_point);
  const stage = activityStage(input.phase);
  const mode =
    input.mode && SESSION_MODE_LABELS[input.mode]
      ? SESSION_MODE_LABELS[input.mode]
      : undefined;

  if (stage && point) return `${stage} · ${point}`;
  if (stage) return stage;
  if (mode && point) return `${mode} · ${point}`;
  if (point) return point;
  if (mode) return mode;
  if (input.phase && !looksLikeEntityWait(input.phase)) return input.phase;
  return "Starting measurement";
}

/** The same, without capitalising — for labels that sit inside a sentence. */
export function words(value: string): string {
  return value.replaceAll("_", " ");
}

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** A duration in whole units, rounded up so an estimate never reads as already finished. */
export function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0 sec";
  if (seconds < MINUTE) return `${Math.ceil(seconds)} sec`;

  let leftover = Math.ceil(seconds);
  let days = Math.floor(leftover / DAY);
  leftover %= DAY;
  let hours = Math.floor(leftover / HOUR);
  leftover %= HOUR;
  let minutes = Math.ceil(leftover / MINUTE);
  if (minutes === 60) {
    minutes = 0;
    hours += 1;
  }
  if (hours === 24) {
    hours = 0;
    days += 1;
  }

  if (days) {
    const parts = [`${days} ${days === 1 ? "day" : "days"}`];
    if (hours) parts.push(`${hours} hr`);
    if (minutes) parts.push(`${minutes} min`);
    return parts.join(" ");
  }
  if (hours) return `${hours} hr ${minutes} min`;
  return `${minutes} min`;
}

/** A remaining time, in the coarser shape the running view shows while a measurement is in flight. */
export function remaining(seconds?: number | null): string {
  if (seconds == null) return "Calculating";
  const minutes = Math.ceil(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} hr ${minutes % 60} min`;
}

export function timestamp(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

/** Time-of-day only, with seconds -- for the running view's log pane, where lines arrive
 * fast enough that a full date/time stamp would be both redundant and too wide. */
export function logTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(new Date(value));
}

export function calibrationDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
}

export function resistance(ohms: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(ohms);
}
