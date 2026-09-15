import type { PowerMeterDiagnostic } from "./api";
import type { LutMode, MeasurementRequest, MeasureType, OperatingPoint, SweepCoverage } from "./measurement";

export type SessionState =
  | "idle"
  | "validating"
  | "ready"
  | "awaiting_confirmation"
  | "running"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed"
  | "resumable";

export interface PreflightResponse {
  valid: boolean;
  warnings: string[];
  estimated_variations?: number | null;
  estimated_duration_seconds?: number | null;
  supported_modes?: LutMode[] | null;
  power_meter_diagnostic?: PowerMeterDiagnostic | null;
  battery_level_entity_id?: string | null;
  battery_level_attribute?: string | null;
  light_load_probe?: {
    checked_variations: number;
    minimum_aggregate_power_w: number;
    points: {
      label: string;
      mode: LutMode;
      power_w: number;
    }[];
  } | null;
}

export interface SessionProgress {
  completed: number;
  total: number;
  skipped?: number;
  already_measured?: number;
  percent?: number;
  elapsed_seconds?: number | null;
  estimated_remaining_seconds?: number | null;
}

export interface SessionSnapshot {
  session_id?: string;
  state: SessionState;
  can_analyse?: boolean;
  created_at?: string;
  updated_at?: string;
  phase?: string | null;
  activity_reason?: string | null;
  confirmation_message?: string | null;
  confirmation_action?: string | null;
  mode?: string | null;
  run_started_at?: string | null;
  wait_ends_at?: string | null;
  wait_seconds?: number | null;
  progress?: SessionProgress;
  warnings?: string[];
  error?: { code?: string; message: string } | string | null;
  summary?: Record<string, string> | null;
  request?: MeasurementRequest;
  operating_point?: OperatingPoint | null;
  calibration_sample?: {
    power: number;
    resistance: number;
    voltage: number;
  } | null;
  entity_states?: Record<string, string>;
  sweep_coverage?: SweepCoverage | null;
}

export interface SessionSummary {
  session_id: string;
  state: SessionState;
  created_at: string;
  updated_at: string;
  measure_type: MeasureType;
  model_id: string;
  product_name: string;
  manufacturer?: string;
  measure_device: string;
  completed: number;
  total: number;
  percent: number;
  can_resume: boolean;
  can_refine?: boolean;
  can_merge?: boolean;
  can_analyse?: boolean;
  file_count: number;
  size: number;
  active: boolean;
  family_key?: string;
  modes?: string[];
  run_started_at?: string | null;
  duration_seconds?: number | null;
  already_measured?: number;
  measured?: number;
  seed_session_id?: string | null;
}

export interface SessionFile {
  name: string;
  size: number;
  media_type: string;
}

export interface PlotStat {
  label: string;
  value: string;
}

export interface PlotPoint {
  x: number;
  y: number;
  color: string | null;
  inherited?: boolean;
  id?: string | null;
  rail?: string | null;
  stats?: PlotStat[];
  ignored?: boolean;
  editable?: boolean;
  interest?: string | null;
  z?: number | null;
}

export interface PlotMarker {
  x: number;
  label: string;
}

export type PlotPointAction = "edit" | "fix_outlier" | "ignore" | "unignore" | "delete";

export interface PlotPointActionDetail {
  pointId: string;
  action: PlotPointAction;
  watt?: number;
}

export interface PlotHoldDetail {
  pointId: string;
  plotId: string;
  hue: number | null;
  bri: number | null;
  mired: number | null;
}

export interface PlotYViewDetail {
  pair: "hs" | "color_temp";
  y: { minY: number; maxY: number } | null;
}

export interface PlotSeries {
  label: string | null;
  color: string | null;
  points: PlotPoint[];
}

export interface PlotSpec {
  id: string;
  title: string;
  kind: "scatter" | "line" | "cylinder";
  x_label: string;
  y_label: string;
  source: string;
  series: PlotSeries[];
  x_min?: number | null;
  x_max?: number | null;
  markers?: PlotMarker[];
}

export interface PlotCollection {
  partial: boolean;
  plots: PlotSpec[];
  warnings: string[];
  editable?: boolean;
}

export interface OcrPreviewLastFrame {
  accepted: boolean;
  reason: string | null;
  power: number | null;
  voltage: number | null;
  current: number | null;
  pf: number | null;
  raw: Record<string, string>;
  timestamp: number;
}

export interface OcrPreviewState {
  power: number | null;
  frame_age: number | null;
  accepted_age: number | null;
  fps: number | null;
  angle: number | null;
  source_error: string | null;
  last: OcrPreviewLastFrame | null;
  frames?: number;
  accepted?: number;
  rejected?: number;
  relocations?: number;
  [count: string]: unknown;
}

export interface MeterPreviewStart {
  preview_id: string | null;
  labels: string[];
}

export interface LogEntry {
  time: string;
  message: string;
  sequence?: number;
}

/** A collection with nothing plotted yet, used as the initial and the reset value. */
export function emptyPlots(warnings: string[] = []): PlotCollection {
  return { partial: false, plots: [], warnings };
}

/**
 * Payload of a regular session event. Progress, phase and state all reach the app through the
 * snapshot that rides along with the event, so only live/log fields are read from the payload.
 */
export interface SessionEventData {
  message?: string;
  power?: number;
  resistance?: number;
  voltage?: number;
  states?: Record<string, string>;
}

/** Event types the stream subscribes to. The server sends each one as its own SSE event name. */
export const REGULAR_SESSION_EVENT_TYPES = [
  "phase",
  "progress",
  "state",
  "warning",
  "log",
  "checkpoint",
  "heartbeat",
  "sample",
  "calibration_sample",
  "entity_states",
] as const;

export const SESSION_EVENT_TYPES = [...REGULAR_SESSION_EVENT_TYPES, "operating_point"] as const;

interface RegularSessionEvent {
  sequence: number;
  type: (typeof REGULAR_SESSION_EVENT_TYPES)[number];
  created_at?: string;
  data: SessionEventData;
  snapshot?: SessionSnapshot;
}

interface OperatingPointSessionEvent {
  sequence: number;
  type: "operating_point";
  created_at?: string;
  data: OperatingPoint;
  snapshot?: SessionSnapshot;
}

export type SessionEvent = RegularSessionEvent | OperatingPointSessionEvent;
