import {
  DEFAULT_SHELLY_USERNAME,
  POWER_METERS,
  POWER_METER_LIST,
  describe as describeMeter,
  hasVoltageReading,
  isAddressed,
  settingsFromForm,
  singleMeterSpecFromWitnessDraft,
  specFromRequest,
  specFromSettings,
  summarize,
  witnessSpecFromSettings,
} from "./power-meter";
import type { MeterContext } from "./power-meter";
import type { AppSettings, MeasurementRequest, PowerMeterSpec, PowerMeterType, WitnessSettings } from "./types";

const settings: AppSettings = {
  default_power_entity_id: "sensor.plug_power",
  default_measure_device: "Shelly Plug S",
  power_meter: "hass",
  shelly_ip: "192.0.2.20",
  shelly_username: "operator",
  kasa_ip: "192.0.2.30",
  mystrom_ip: "192.0.2.40",
  tasmota_ip: "192.0.2.41",
  tuya_device_id: "abc123",
  tuya_device_ip: "192.0.2.42",
  tuya_version: "3.4",
  owon_port: "/dev/ttyUSB0",
  owon_baudrate: 9600,
  owon_channel: "1",
  ocr_source: "http://camera/stream",
  witnesses: [],
  fast_test_mode: false,
  measurement_defaults: { sleep_time: 1, sample_count: 1, sleep_time_sample: 1, max_retries: 5, max_nudges: 0 },
};

/** Every settings-derived field an address-keyed meter can own, all defaulted to null so a
 * test only needs to name what its own meter type actually sets. */
const NO_ADDRESS_FIELDS = {
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
};

const context: MeterContext = {
  powers: [{ entity_id: "sensor.plug_power", name: "Plug power", related_voltage_entity_id: "sensor.plug_voltage" }],
  voltages: [{ entity_id: "sensor.plug_voltage", name: "Plug voltage" }],
};

const request = (power_meter: PowerMeterSpec): MeasurementRequest => ({
  measure_type: "average",
  model_id: "measurement",
  product_name: "Measurement",
  measure_device: "Shelly Plug S",
  generate_model: false,
  duration: 60,
  parameters: settings.measurement_defaults as MeasurementRequest["parameters"],
  resume_policy: "new",
  power_meter,
});

const form = (entries: Record<string, string | undefined>): FormData => {
  const data = new FormData();
  for (const [name, value] of Object.entries(entries)) if (value !== undefined) data.append(name, value);
  return data;
};

const TYPES: PowerMeterType[] = ["hass", "shelly", "kasa", "mystrom", "tasmota", "tuya", "owh98xx", "ocr", "dummy"];

describe("power meter registry", () => {
  it("describes every type of meter exactly once, in picker order", () => {
    expect(POWER_METER_LIST.map((meter) => meter.type)).toEqual(TYPES);
    for (const type of TYPES) expect(POWER_METERS[type].type).toBe(type);
  });

  it("gives every meter a distinct label for the settings picker", () => {
    const labels = POWER_METER_LIST.map((meter) => meter.label);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it.each([
    { type: "hass" as const, expected: { type: "hass", entity_id: "sensor.plug_power", voltage_entity_id: "sensor.plug_voltage" } },
    { type: "shelly" as const, expected: { type: "shelly", device_ip: "192.0.2.20", username: "operator" } },
    { type: "kasa" as const, expected: { type: "kasa", device_ip: "192.0.2.30" } },
    { type: "mystrom" as const, expected: { type: "mystrom", device_ip: "192.0.2.40" } },
    { type: "tasmota" as const, expected: { type: "tasmota", device_ip: "192.0.2.41" } },
    { type: "tuya" as const, expected: { type: "tuya", device_id: "abc123", device_ip: "192.0.2.42", version: "3.4" } },
    { type: "owh98xx" as const, expected: { type: "owh98xx", port: "/dev/ttyUSB0", baudrate: 9600, channel: "1" } },
    { type: "ocr" as const, expected: { type: "ocr", source: "http://camera/stream", layout: "pr10", preview_host: "127.0.0.1", preview_port: 8765 } },
    { type: "dummy" as const, expected: { type: "dummy" } },
  ])("builds the $type meter the saved settings configure", ({ type, expected }) => {
    expect(specFromSettings({ ...settings, power_meter: type }, context)).toEqual(expected);
  });

  it("wraps the primary in a composite once at least one witness is configured", () => {
    const witnesses: WitnessSettings[] = [
      { meter: { type: "shelly", device_ip: "192.0.2.50" }, position: "none", offset_w: 1, tolerance_w: 0.5, tolerance_pct: 2, required: true },
    ];
    const spec = specFromSettings({ ...settings, witnesses }, context);
    expect(spec).toEqual({
      type: "composite",
      primary: { type: "hass", entity_id: "sensor.plug_power", voltage_entity_id: "sensor.plug_voltage" },
      witnesses: [
        { meter: { type: "shelly", device_ip: "192.0.2.50", username: "admin" }, position: "none", offset_w: 1, tolerance_w: 0.5, tolerance_pct: 2, required: true },
      ],
    });
  });

  it("converts a witness draft's meter using its own field names, not the primary's namespaced ones", () => {
    expect(singleMeterSpecFromWitnessDraft({ type: "hass", entity_id: "sensor.witness" }))
      .toEqual({ type: "hass", entity_id: "sensor.witness", voltage_entity_id: null, max_age_seconds: null });
    expect(singleMeterSpecFromWitnessDraft({ type: "shelly", device_ip: "192.0.2.51" }))
      .toEqual({ type: "shelly", device_ip: "192.0.2.51", username: DEFAULT_SHELLY_USERNAME });
    expect(singleMeterSpecFromWitnessDraft({ type: "tuya", device_id: "id1", device_ip: "192.0.2.52" }))
      .toEqual({ type: "tuya", device_id: "id1", device_ip: "192.0.2.52", version: "3.3" });
  });

  it("carries a witness's tolerances and requiredness into its spec unchanged", () => {
    const witness: WitnessSettings = {
      meter: { type: "kasa", device_ip: "192.0.2.53" },
      position: "none",
      offset_w: -2,
      tolerance_w: 1,
      tolerance_pct: 5,
      required: false,
    };
    expect(witnessSpecFromSettings(witness)).toEqual({
      meter: { type: "kasa", device_ip: "192.0.2.53" },
      position: "none",
      offset_w: -2,
      tolerance_w: 1,
      tolerance_pct: 5,
      required: false,
    });
  });

  it("defaults an unconfigured app to an unaddressed Home Assistant sensor", () => {
    const spec = specFromSettings(undefined, context);
    expect(spec).toEqual({ type: "hass", entity_id: "", voltage_entity_id: null });
    expect(isAddressed(spec)).toBe(false);
  });

  it("falls back to the default Shelly username when none is saved", () => {
    const spec = specFromSettings({ ...settings, power_meter: "shelly", shelly_username: undefined }, context);
    expect(spec).toEqual({ type: "shelly", device_ip: "192.0.2.20", username: DEFAULT_SHELLY_USERNAME });
  });

  it("keeps the meter a session was started with, rather than the current default", () => {
    const stored: PowerMeterSpec = { type: "kasa", device_ip: "192.0.2.99" };
    expect(specFromRequest(request(stored), settings, context)).toEqual(stored);
  });

  it("falls back to the saved default when a draft names no meter yet", () => {
    expect(specFromRequest(undefined, settings, context)).toEqual(specFromSettings(settings, context));
  });

  it.each([
    { type: "hass", fields: { default_power_entity_id: "sensor.other" }, owned: { default_power_entity_id: "sensor.other" } },
    { type: "shelly", fields: { shelly_ip: " 192.0.2.21 " }, owned: { shelly_ip: "192.0.2.21" } },
    { type: "kasa", fields: { kasa_ip: "192.0.2.31" }, owned: { kasa_ip: "192.0.2.31" } },
    { type: "mystrom", fields: { mystrom_ip: "192.0.2.41" }, owned: { mystrom_ip: "192.0.2.41" } },
    { type: "tasmota", fields: { tasmota_ip: "192.0.2.42" }, owned: { tasmota_ip: "192.0.2.42" } },
    {
      type: "tuya",
      fields: { tuya_device_id: "id1", tuya_device_ip: "192.0.2.43", tuya_version: "3.4" },
      owned: { tuya_device_id: "id1", tuya_device_ip: "192.0.2.43", tuya_version: "3.4" },
    },
    {
      type: "owh98xx",
      fields: { owon_port: "/dev/ttyUSB0", owon_baudrate: "9600", owon_channel: "2" },
      owned: { owon_port: "/dev/ttyUSB0", owon_baudrate: 9600, owon_channel: "2" },
    },
    { type: "ocr", fields: { ocr_source: "0" }, owned: { ocr_source: "0", ocr_layout: "pr10" } },
    { type: "dummy", fields: {}, owned: {} },
  ])("saves only the address keys the selected $type meter owns", ({ type, fields, owned }) => {
    expect(settingsFromForm(form({ power_meter: type, ...fields }))).toEqual({
      power_meter: type,
      ...NO_ADDRESS_FIELDS,
      ...owned,
    });
  });

  it("ignores addresses left over from a meter that is no longer selected", () => {
    const saved = settingsFromForm(form({ power_meter: "kasa", kasa_ip: "192.0.2.31", shelly_ip: "192.0.2.21" }));
    expect(saved.shelly_ip).toBeNull();
  });

  it.each([
    { type: "hass" as const, addressed: { type: "hass", entity_id: "sensor.plug_power" }, blank: { type: "hass", entity_id: "" } },
    { type: "shelly" as const, addressed: { type: "shelly", device_ip: "192.0.2.20" }, blank: { type: "shelly", device_ip: "" } },
    { type: "kasa" as const, addressed: { type: "kasa", device_ip: "192.0.2.30" }, blank: { type: "kasa", device_ip: "" } },
    { type: "mystrom" as const, addressed: { type: "mystrom", device_ip: "192.0.2.40" }, blank: { type: "mystrom", device_ip: "" } },
    { type: "tasmota" as const, addressed: { type: "tasmota", device_ip: "192.0.2.41" }, blank: { type: "tasmota", device_ip: "" } },
    {
      type: "tuya" as const,
      addressed: { type: "tuya", device_id: "id1", device_ip: "192.0.2.42" },
      blank: { type: "tuya", device_id: "", device_ip: "192.0.2.42" },
    },
    {
      type: "owh98xx" as const,
      addressed: { type: "owh98xx", port: "/dev/ttyUSB0", baudrate: 9600, channel: "1" },
      blank: { type: "owh98xx", port: "", baudrate: 9600, channel: "1" },
    },
  ] satisfies { type: PowerMeterType; addressed: PowerMeterSpec; blank: PowerMeterSpec }[])(
    "treats $type as unaddressed until its own address is filled in",
    ({ addressed, blank }) => {
      expect(isAddressed(addressed)).toBe(true);
      expect(isAddressed(blank)).toBe(false);
    },
  );

  it("needs no address for the synthetic meter or for camera OCR, which has a usable default source", () => {
    expect(isAddressed({ type: "dummy" })).toBe(true);
    expect(isAddressed({ type: "ocr" })).toBe(true);
  });

  it("is unaddressed if any witness in a composite is unaddressed, even when the primary is fine", () => {
    const composite: PowerMeterSpec = {
      type: "composite",
      primary: { type: "hass", entity_id: "sensor.plug_power" },
      witnesses: [{ meter: { type: "shelly", device_ip: "" } }],
    };
    expect(isAddressed(composite)).toBe(false);
  });

  it("has a voltage reading for directly polled meters, and only a paired sensor for Home Assistant", () => {
    expect(hasVoltageReading({ type: "shelly", device_ip: "192.0.2.20" })).toBe(true);
    expect(hasVoltageReading({ type: "kasa", device_ip: "192.0.2.30" })).toBe(true);
    expect(hasVoltageReading({ type: "hass", entity_id: "sensor.plug_power", voltage_entity_id: "sensor.v" })).toBe(true);
    expect(hasVoltageReading({ type: "hass", entity_id: "sensor.plug_power" })).toBe(false);
    expect(hasVoltageReading({ type: "ocr" })).toBe(false);
  });

  it("follows a composite's primary for voltage and dummy-load capability, ignoring its witnesses", () => {
    const composite: PowerMeterSpec = {
      type: "composite",
      primary: { type: "shelly", device_ip: "192.0.2.20" },
      witnesses: [{ meter: { type: "hass", entity_id: "sensor.witness" } }],
    };
    expect(hasVoltageReading(composite)).toBe(true);
  });

  it("names a composite by its primary, noting how many witnesses back it up", () => {
    const composite: PowerMeterSpec = {
      type: "composite",
      primary: { type: "shelly", device_ip: "192.0.2.20" },
      witnesses: [{ meter: { type: "kasa", device_ip: "192.0.2.30" } }, { meter: { type: "hass", entity_id: "sensor.w" } }],
    };
    expect(summarize(composite)).toBe("Shelly plug (+2 witnesses)");
    expect(describeMeter(composite, context).detail).toContain("witnessed by 2 meters");
  });

  it("describes a Home Assistant sensor with its paired voltage sensor", () => {
    expect(describeMeter(specFromSettings(settings, context), context)).toEqual({
      source: "Plug power · sensor.plug_power",
      detail: "Voltage: Plug voltage · sensor.plug_voltage",
    });
  });

  it("says so when a Home Assistant sensor has no paired voltage sensor", () => {
    expect(describeMeter({ type: "hass", entity_id: "sensor.plug_power" }, context).detail)
      .toBe("Home Assistant power sensor");
  });

  it.each(TYPES)("always has something to say about a %s meter", (type) => {
    const spec = specFromSettings({ ...settings, power_meter: type }, context);
    const description = describeMeter(spec, context);
    expect(description.source).toBeTruthy();
    expect(description.detail).toBeTruthy();
    expect(summarize(spec)).toBeTruthy();
  });

  it("names a directly polled meter by label on the review screen, and a sensor by entity", () => {
    expect(summarize({ type: "hass", entity_id: "sensor.plug_power" })).toBe("sensor.plug_power");
    expect(summarize({ type: "shelly", device_ip: "192.0.2.20" })).toBe("Shelly plug");
  });

  it("only offers a dummy load on meters that read real power", () => {
    expect(POWER_METERS.dummy.supportsDummyLoad).toBe(false);
    for (const type of TYPES.filter((candidate) => candidate !== "dummy")) {
      expect(POWER_METERS[type].supportsDummyLoad).toBe(true);
    }
  });

  it("only validates meters that have real readings to check", () => {
    expect(POWER_METERS.dummy.validatable).toBe(false);
    expect(POWER_METERS.hass.validatable).toBe(true);
  });
});
