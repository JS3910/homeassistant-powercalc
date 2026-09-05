import { formText, formTextOrNull } from "./form";
import type {
  AppSettings,
  EntityDescriptor,
  MeasurementRequest,
  PowerMeterSpec,
  PowerMeterType,
  SingleMeterSpec,
  WitnessMeterSettings,
  WitnessSettings,
  WitnessSpec,
} from "./types";

/** Username Shelly devices are reached with unless the user configured another one. */
export const DEFAULT_SHELLY_USERNAME = "admin";

/** Entities the app knows about, which a meter may need in order to address or describe itself. */
export interface MeterContext {
  powers: EntityDescriptor[];
  voltages: EntityDescriptor[];
}

/** How a meter is presented: a headline source and a supporting detail line. */
export interface MeterDescription {
  source: string;
  detail: string;
}

/** The settings keys that name where readings come from. Each meter owns exactly the ones it uses. */
export interface PowerMeterSettings {
  default_power_entity_id: string | null;
  shelly_ip: string | null;
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
}

/** The spec variant belonging to one type of meter. */
type SpecOf<T extends PowerMeterType> = Extract<SingleMeterSpec, { type: T }>;

/**
 * Everything the app needs to know about one type of power meter.
 *
 * Adding a meter means adding its variant to {@link PowerMeterSpec} and one entry here; the type
 * of {@link POWER_METERS} then makes the compiler name everything left out. The settings form's
 * own inputs are the one part that lives elsewhere, in `settings-view`, because they need that
 * view's handlers and credential state.
 */
export interface PowerMeterDescriptor<T extends PowerMeterType = PowerMeterType> {
  type: T;
  /** Name shown in the settings picker, and used wherever a meter is named without its address. */
  label: string;
  /** Whether readings can be validated up front. A synthetic meter has nothing to check. */
  validatable: boolean;
  /** Whether choosing this meter should look for devices on the network. */
  discoverable: boolean;
  /** Whether a resistive dummy load can be measured against this meter at all. */
  supportsDummyLoad: boolean;
  /** What the user should know about reading quality with this meter, when there is anything. */
  qualityNote?: string;
  /** The meter the saved app settings configure. */
  fromSettings(settings: AppSettings | undefined, context: MeterContext): SpecOf<T>;
  /** The settings keys this meter owns, read out of the settings form. Anything omitted saves as null. */
  settingsFromForm(form: FormData): Partial<PowerMeterSettings>;
  /** Whether the address this meter needs is filled in. */
  isAddressed(spec: SpecOf<T>): boolean;
  /** Whether a voltage reading is available, which dummy-load correction depends on. */
  hasVoltageReading(spec: SpecOf<T>): boolean;
  describe(spec: SpecOf<T>, context: MeterContext): MeterDescription;
}

/** Shared by every meter Powercalc reads over the network rather than through Home Assistant. */
const POLLED_DIRECTLY = "Powercalc polls this device directly, so Home Assistant sensor resolution and update-frequency checks do not apply.";

/** Every type of meter, in the order the settings picker offers them. */
export const POWER_METERS: { [T in PowerMeterType]: PowerMeterDescriptor<T> } = {
  hass: {
    type: "hass",
    label: "Home Assistant sensor",
    validatable: true,
    discoverable: false,
    supportsDummyLoad: true,
    qualityNote: "For reliable profiles, use a sensor with at least 0.1 W reported resolution and updates every 5 seconds or faster. Updates within 2 seconds are recommended.",
    fromSettings: (settings, context) => {
      const entityId = settings?.default_power_entity_id ?? "";
      return {
        type: "hass",
        entity_id: entityId,
        // The paired voltage sensor is what makes dummy-load correction possible.
        voltage_entity_id: relatedVoltageEntityId(context.powers, entityId) || null,
      };
    },
    settingsFromForm: (form) => ({ default_power_entity_id: formTextOrNull(form, "default_power_entity_id") }),
    isAddressed: (spec) => Boolean(spec.entity_id),
    hasVoltageReading: (spec) => Boolean(spec.voltage_entity_id),
    describe: (spec, context) => {
      const entity = context.powers.find((candidate) => candidate.entity_id === spec.entity_id);
      const voltage = context.voltages.find((candidate) => candidate.entity_id === spec.voltage_entity_id);
      const voltageName = voltage ? `${voltage.name} · ` : "";
      const voltageDetail = `Voltage: ${voltageName}${spec.voltage_entity_id}`;
      return {
        source: entity ? `${entity.name} · ${entity.entity_id}` : spec.entity_id,
        detail: spec.voltage_entity_id ? voltageDetail : "Home Assistant power sensor",
      };
    },
  },

  shelly: {
    type: "shelly",
    label: "Shelly plug",
    validatable: true,
    discoverable: true,
    supportsDummyLoad: true,
    qualityNote: POLLED_DIRECTLY,
    fromSettings: (settings) => ({
      type: "shelly",
      device_ip: settings?.shelly_ip ?? "",
      username: settings?.shelly_username ?? DEFAULT_SHELLY_USERNAME,
    }),
    settingsFromForm: (form) => ({ shelly_ip: formTextOrNull(form, "shelly_ip") }),
    isAddressed: (spec) => Boolean(spec.device_ip),
    // Powercalc reads voltage straight off the device.
    hasVoltageReading: () => true,
    describe: (spec) => ({ source: "Shelly power meter", detail: spec.device_ip }),
  },

  kasa: {
    type: "kasa",
    label: "Kasa smart plug",
    validatable: true,
    discoverable: false,
    supportsDummyLoad: true,
    qualityNote: POLLED_DIRECTLY,
    fromSettings: (settings) => ({ type: "kasa", device_ip: settings?.kasa_ip ?? "" }),
    settingsFromForm: (form) => ({ kasa_ip: formTextOrNull(form, "kasa_ip") }),
    isAddressed: (spec) => Boolean(spec.device_ip),
    hasVoltageReading: () => true,
    describe: (spec) => ({ source: "Kasa power meter", detail: spec.device_ip }),
  },

  mystrom: {
    type: "mystrom",
    label: "myStrom switch",
    validatable: true,
    discoverable: false,
    supportsDummyLoad: true,
    qualityNote: POLLED_DIRECTLY,
    fromSettings: (settings) => ({ type: "mystrom", device_ip: settings?.mystrom_ip ?? "" }),
    settingsFromForm: (form) => ({ mystrom_ip: formTextOrNull(form, "mystrom_ip") }),
    isAddressed: (spec) => Boolean(spec.device_ip),
    hasVoltageReading: () => true,
    describe: (spec) => ({ source: "myStrom switch", detail: spec.device_ip }),
  },

  tasmota: {
    type: "tasmota",
    label: "Tasmota device",
    validatable: true,
    discoverable: false,
    supportsDummyLoad: true,
    qualityNote: POLLED_DIRECTLY,
    fromSettings: (settings) => ({ type: "tasmota", device_ip: settings?.tasmota_ip ?? "" }),
    settingsFromForm: (form) => ({ tasmota_ip: formTextOrNull(form, "tasmota_ip") }),
    isAddressed: (spec) => Boolean(spec.device_ip),
    hasVoltageReading: () => true,
    describe: (spec) => ({ source: "Tasmota device", detail: spec.device_ip }),
  },

  tuya: {
    type: "tuya",
    label: "Tuya device",
    validatable: true,
    discoverable: false,
    supportsDummyLoad: true,
    qualityNote: POLLED_DIRECTLY,
    fromSettings: (settings) => ({
      type: "tuya",
      device_id: settings?.tuya_device_id ?? "",
      device_ip: settings?.tuya_device_ip ?? "",
      version: settings?.tuya_version ?? "3.3",
    }),
    settingsFromForm: (form) => ({
      tuya_device_id: formTextOrNull(form, "tuya_device_id"),
      tuya_device_ip: formTextOrNull(form, "tuya_device_ip"),
      tuya_version: formText(form, "tuya_version") || "3.3",
    }),
    isAddressed: (spec) => Boolean(spec.device_id && spec.device_ip),
    hasVoltageReading: () => true,
    describe: (spec) => ({ source: "Tuya device", detail: spec.device_ip }),
  },

  owh98xx: {
    type: "owh98xx",
    label: "Owon OWH98XX (serial)",
    validatable: true,
    discoverable: false,
    supportsDummyLoad: true,
    qualityNote: POLLED_DIRECTLY,
    fromSettings: (settings) => ({
      type: "owh98xx",
      port: settings?.owon_port ?? "",
      baudrate: settings?.owon_baudrate ?? 9600,
      channel: settings?.owon_channel ?? "1",
    }),
    settingsFromForm: (form) => ({
      owon_port: formTextOrNull(form, "owon_port"),
      owon_baudrate: Number(formText(form, "owon_baudrate") || "9600"),
      owon_channel: (formText(form, "owon_channel") || "1") as "1" | "2",
    }),
    isAddressed: (spec) => Boolean(spec.port && spec.baudrate && spec.channel),
    hasVoltageReading: () => true,
    describe: (spec) => ({ source: "Owon OWH98XX", detail: `${spec.port} · channel ${spec.channel}` }),
  },

  ocr: {
    type: "ocr",
    label: "Camera OCR (meter display)",
    validatable: true,
    discoverable: false,
    supportsDummyLoad: true,
    qualityNote: "Reads a physical meter's display through a camera. Voltage and current, when the display shows them, are cross-checked against power before a reading is accepted.",
    fromSettings: (settings) => ({
      type: "ocr",
      source: settings?.ocr_source ?? "0",
      layout: settings?.ocr_layout ?? "pr10",
      preview_host: settings?.ocr_preview_host ?? "127.0.0.1",
      preview_port: settings?.ocr_preview_port ?? 8765,
    }),
    settingsFromForm: (form) => ({
      ocr_source: formText(form, "ocr_source") || "0",
      ocr_layout: formText(form, "ocr_layout") || "pr10",
    }),
    isAddressed: () => true,
    hasVoltageReading: () => false,
    describe: (spec) => ({ source: "Camera OCR", detail: spec.source ?? "0" }),
  },

  dummy: {
    type: "dummy",
    label: "Synthetic test meter",
    validatable: false,
    discoverable: false,
    supportsDummyLoad: false,
    fromSettings: () => ({ type: "dummy" }),
    settingsFromForm: () => ({}),
    isAddressed: () => true,
    hasVoltageReading: () => false,
    describe: () => ({ source: "Synthetic test meter", detail: "No external readings are used." }),
  },
};

/** Meters in picker order, for the settings form to list. See {@link meterFor} on the cast. */
export const POWER_METER_LIST: PowerMeterDescriptor[] = Object.values(POWER_METERS) as PowerMeterDescriptor[];

/** The descriptor for a meter. */
export function meterFor(type: PowerMeterType): PowerMeterDescriptor {
  // Sound because the record is keyed by exactly this union; the cast only drops the per-type spec
  // narrowing, which nothing outside this module needs.
  return POWER_METERS[type] as PowerMeterDescriptor;
}

/** A witness draft's meter, converted to the spec shape a session request sends.
 * Field names differ from {@link PowerMeterSettings} (`entity_id` vs `default_power_entity_id`,
 * and so on) because a witness draft is not namespaced by type the way the top-level settings
 * fields are — there can be several witnesses of the same type at once. */
export function singleMeterSpecFromWitnessDraft(draft: WitnessMeterSettings): SingleMeterSpec {
  switch (draft.type) {
    case "hass":
      return {
        type: "hass",
        entity_id: draft.entity_id ?? "",
        voltage_entity_id: draft.voltage_entity_id ?? null,
        max_age_seconds: draft.max_age_seconds ?? null,
      };
    case "shelly":
      return { type: "shelly", device_ip: draft.device_ip ?? "", username: draft.username ?? DEFAULT_SHELLY_USERNAME };
    case "kasa":
      return { type: "kasa", device_ip: draft.device_ip ?? "" };
    case "mystrom":
      return { type: "mystrom", device_ip: draft.device_ip ?? "" };
    case "tasmota":
      return { type: "tasmota", device_ip: draft.device_ip ?? "" };
    case "tuya":
      return { type: "tuya", device_id: draft.device_id ?? "", device_ip: draft.device_ip ?? "", version: draft.version ?? "3.3" };
    case "owh98xx":
      return { type: "owh98xx", port: draft.port ?? "", baudrate: draft.baudrate ?? 9600, channel: draft.channel ?? "1" };
    case "ocr":
      return {
        type: "ocr",
        source: draft.source ?? "0",
        layout: draft.layout ?? "pr10",
        preview_host: draft.preview_host ?? "127.0.0.1",
        preview_port: draft.preview_port ?? 8765,
      };
    case "dummy":
    default:
      return { type: "dummy" };
  }
}

/** A saved witness, converted to the spec shape a session request sends. */
export function witnessSpecFromSettings(witness: WitnessSettings): WitnessSpec {
  return {
    meter: singleMeterSpecFromWitnessDraft(witness.meter),
    offset_w: witness.offset_w,
    tolerance_w: witness.tolerance_w,
    tolerance_pct: witness.tolerance_pct,
    required: witness.required,
  };
}

/** The meter the saved app settings configure: the primary alone, or wrapped as a composite
 * once at least one witness is configured (a composite cannot have zero witnesses). */
export function specFromSettings(settings: AppSettings | undefined, context: MeterContext): PowerMeterSpec {
  const primary = meterFor(settings?.power_meter ?? "hass").fromSettings(settings, context);
  const witnesses = settings?.witnesses ?? [];
  if (!witnesses.length) return primary;
  return { type: "composite", primary, witnesses: witnesses.map(witnessSpecFromSettings) };
}

/** The meter a draft reads from: the one its request already names, else the saved default. */
export function specFromRequest(
  request: MeasurementRequest | undefined,
  settings: AppSettings | undefined,
  context: MeterContext,
): PowerMeterSpec {
  return request?.power_meter ?? specFromSettings(settings, context);
}

/** The settings payload the form describes: the selected meter's keys, and null/default for the
 * rest, so switching the primary type clears out whatever the previous type had entered.
 * `witnesses` is not form-derived; the settings view assembles it separately from its own
 * repeatable-row state and merges it into this result before saving. */
export function settingsFromForm(form: FormData): PowerMeterSettings & { power_meter: PowerMeterType } {
  const type = (formText(form, "power_meter") || "hass") as PowerMeterType;
  return {
    power_meter: type,
    default_power_entity_id: null,
    shelly_ip: null,
    kasa_ip: null,
    hass_max_age_seconds: null,
    mystrom_ip: null,
    tasmota_ip: null,
    tuya_device_id: null,
    tuya_device_ip: null,
    owon_port: null,
    owon_baudrate: null,
    owon_channel: null,
    ...meterFor(type).settingsFromForm(form),
  };
}

/** Whether this meter names a usable address; a composite also needs every witness addressed. */
export function isAddressed(spec: PowerMeterSpec): boolean {
  if (spec.type === "composite") {
    return isAddressed(spec.primary) && spec.witnesses.every((witness) => isAddressed(witness.meter));
  }
  return meterFor(spec.type).isAddressed(spec);
}

/** Whether dummy-load correction can read the voltage it needs from this meter. A composite
 * reads voltage from its primary only; witnesses exist to cross-check power, not to supply it. */
export function hasVoltageReading(spec: PowerMeterSpec): boolean {
  if (spec.type === "composite") return hasVoltageReading(spec.primary);
  return meterFor(spec.type).hasVoltageReading(spec);
}

/** Whether this meter supports dummy-load calibration; a composite follows its primary. */
export function supportsDummyLoad(spec: PowerMeterSpec): boolean {
  return meterFor(spec.type === "composite" ? spec.primary.type : spec.type).supportsDummyLoad;
}

/** How the meter is described to the user. */
export function describe(spec: PowerMeterSpec, context: MeterContext): MeterDescription {
  if (spec.type === "composite") {
    const primary = describe(spec.primary, context);
    const count = spec.witnesses.length;
    return { source: primary.source, detail: `${primary.detail} — witnessed by ${count} meter${count === 1 ? "" : "s"}` };
  }
  return meterFor(spec.type).describe(spec, context);
}

/** The meter as one line on the review screen, where the entity catalogue is not at hand. */
export function summarize(spec: PowerMeterSpec): string {
  if (spec.type === "composite") {
    const count = spec.witnesses.length;
    return `${summarize(spec.primary)} (+${count} witness${count === 1 ? "" : "es"})`;
  }
  return spec.type === "hass" ? spec.entity_id : meterFor(spec.type).label;
}

/** The voltage sensor Home Assistant associates with a power sensor, when there is one. */
export function relatedVoltageEntityId(powers: EntityDescriptor[], powerEntityId: string): string {
  return powers.find((entity) => entity.entity_id === powerEntityId)?.related_voltage_entity_id ?? "";
}
