import type { AppMeasurementDefaults, PowerMeterType, WitnessPosition } from "./measurement";

export type SettingsSection = "power_meter" | "profile" | "measure_tuning" | "github";

export interface AppSettings {
  default_power_entity_id: string | null;
  default_measure_device: string | null;
  default_measure_device_firmware?: string | null;
  default_contributor_name?: string | null;
  default_contributor_github?: string | null;
  default_contributor_email?: string | null;
  power_meter: PowerMeterType | null;
  shelly_ip: string | null;
  shelly_username?: string;
  shelly_password_configured?: boolean;
  kasa_ip: string | null;
  hass_max_age_seconds?: number | null;
  mystrom_ip?: string | null;
  tasmota_ip?: string | null;
  tuya_device_id?: string | null;
  tuya_device_ip?: string | null;
  tuya_version?: string;
  owon_port?: string | null;
  owon_baudrate?: number | null;
  owon_timeout?: number;
  owon_channel?: "1" | "2" | null;
  ocr_source?: string;
  ocr_layout?: string;
  ocr_preview_host?: string;
  ocr_preview_port?: number | null;
  ocr_window_seconds?: number;
  ocr_stale_after_seconds?: number;
  ocr_crosscheck_tolerance_pct?: number;
  ocr_min_current_for_crosscheck?: number;
  witnesses?: WitnessSettings[];
  fast_test_mode: boolean;
  measurement_defaults: AppMeasurementDefaults;
}

export interface AppSettingsUpdate extends AppSettings {
  shelly_password?: string | null;
  clear_shelly_password?: boolean;
}

export interface WitnessMeterSettings {
  type: PowerMeterType;
  entity_id?: string | null;
  voltage_entity_id?: string | null;
  max_age_seconds?: number | null;
  device_ip?: string | null;
  username?: string;
  device_id?: string | null;
  version?: string;
  port?: string | null;
  baudrate?: number | null;
  timeout?: number;
  channel?: "1" | "2" | null;
  source?: string;
  layout?: string;
  preview_host?: string;
  preview_port?: number | null;
  window_seconds?: number;
  stale_after_seconds?: number;
  crosscheck_tolerance_pct?: number;
  min_current_for_crosscheck?: number;
}

export interface WitnessSettings {
  meter: WitnessMeterSettings;
  position: WitnessPosition;
  offset_w: number;
  tolerance_w: number;
  tolerance_pct: number;
  required: boolean;
}
