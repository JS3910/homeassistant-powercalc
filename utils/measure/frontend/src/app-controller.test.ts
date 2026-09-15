import { ApiError } from "./api-client";
import { MeasureAppController, mergeLogEntries } from "./app-controller";
import type { EventConnection, MeasureAppApi, MeasureAppState } from "./app-controller";
import type { AppHistory, HistoryLocation } from "./app-history";
import type { PowerMeterDiagnostic, SessionEvent, SessionSummary } from "./types";

const measurementDefaults = {
  sleep_time: 1,
  sample_count: 2,
  sleep_time_sample: 1,
  max_retries: 5,
  max_nudges: 0,
};
const settings = {
  default_power_entity_id: null,
  default_measure_device: null,
  power_meter: "hass" as const,
  shelly_ip: null,
  kasa_ip: null,
  fast_test_mode: false,
  measurement_defaults: measurementDefaults,
};
const capabilities = {
  runtime_version: "v0.2.1:app",
  modes: ["brightness" as const],
  defaults: {
    ...measurementDefaults,
    bri_bri_steps: 1,
    ct_bri_steps: 5,
    ct_mired_divisions: 10,
    hs_bri_steps: 32,
    hs_hue_divisions: 2731,
    hs_sat_divisions: 5,
    min_brightness: 1,
    sleep_initial: 10,
    sleep_standby: 20,
    effect_bri_steps: 40,
    measure_time_effect: 180,
    measure_time_effect_min: 20,
  },
};

function sessionSummary(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: "session-1",
    state: "completed",
    created_at: "2026-08-13T10:00:00Z",
    updated_at: "2026-08-13T10:05:00Z",
    measure_type: "light",
    model_id: "LCT010",
    product_name: "Hue lamp",
    measure_device: "Desk lamp",
    completed: 1,
    total: 1,
    percent: 100,
    can_resume: false,
    file_count: 1,
    size: 10,
    active: false,
    ...overrides,
  };
}

function state(): MeasureAppState {
  return {
    view: "loading",
    errorMessage: "",
    busy: false,
    busyDetail: "",
    connectedToEvents: false,
    sessions: [],
    files: [],
    plotCollection: { partial: false, plots: [], warnings: [] },
    ocrPreviewLabels: [],
    logs: [],
    samples: [],
    lights: [],
    powers: [],
    voltages: [],
    definitions: [],
    dummyLoadCalibration: null,
    dummyLoadCalibrationError: "",
    measureDevices: [],
    measureDevicesLoading: false,
    measureDevicesError: "",
    deviceSpecificationFields: {},
    contributionBusy: false,
    contributionAuthBusy: false,
    contributionError: "",
    contributionAuthError: "",
    deviceEntities: {},
    deviceEntityErrors: {},
    testingPowerMeter: false,
    shellyDiscoveryDevices: [],
    discoveringShellys: false,
    shellyDiscoveryError: "",
    meterPreviewId: null,
    meterPreviewLabels: [],
  };
}

function api(overrides: Partial<MeasureAppApi> = {}): MeasureAppApi {
  return {
    getCapabilities: async () => capabilities,
    getMeasureDefinitions: async () => [],
    getMeasureDevices: async () => ({ devices: [] }),
    getManufacturers: async () => ({ manufacturers: [] }),
    getDeviceSpecifications: async () => ({ device_types: {} }),
    getSettings: async () => settings,
    getOcrPreviewLabels: async () => [],
    getContributionAuth: async () => ({ connected: false }),
    getContributionStatus: async () => ({ state: "idle" as const }),
    startContributionDeviceAuth: async () => ({
      flow_id: "flow-1",
      user_code: "ABCD-EFGH",
      verification_uri: "https://github.com/login/device",
      expires_in: 900,
      interval: 5,
    }),
    getContributionDeviceAuth: async () => ({ status: "pending" }),
    saveContributionToken: async () => ({
      connected: true,
      method: "token",
      identity: { login: "octocat" },
    }),
    disconnectContributionAuth: async () => ({ connected: false }),
    saveSettings: async (value) => value,
    testPowerMeter: async () => ({
      success: true,
      power: 1,
      status: "good",
      precision_decimals: 1,
      max_report_interval_seconds: 2,
      reports_observed: 3,
      duration_seconds: 4,
      precision_status: "good",
      update_interval_status: "good",
      messages: [],
    }),
    getShellyDevices: async () => ({
      available: true,
      message: null,
      devices: [],
    }),
    startMeterPreview: async () => ({ preview_id: null, labels: [] }),
    stopMeterPreview: async () => undefined,
    getAllEntities: async () => [],
    getEntityCatalog: async () => ({ lights: [], powers: [], voltages: [] }),
    getEntitiesByDomain: async () => [],
    getEntitiesByDeviceClass: async () => [],
    getDummyLoadCalibration: async () => null,
    preflight: async () => ({ valid: true, warnings: [] }),
    preflightProbe: async () => {
      throw new Error("preflightProbe should not run without probe_steps");
    },
    preflightProbeComplete: async () => {
      throw new Error("preflightProbeComplete should not run without probe_steps");
    },
    estimate: async () => ({
      modes: [],
      total_points: 0,
      max_duration_seconds: 0,
      used_default_range: true,
    }),
    start: async () => ({ state: "running" }),
    getSessions: async () => [],
    getSession: async () => ({ state: "idle" }),
    getSessionLogs: async () => [],
    deleteSession: async () => undefined,
    cancel: async () => ({ state: "cancelled" }),
    confirm: async () => ({ state: "running" }),
    resume: async () => ({ state: "running" }),
    analyse: async () => ({ state: "completed" }),
    getJsonFile: async () => ({}),
    previewMerge: async () => ({
      left: "",
      right: "",
      model_id_warning: false,
      modes: {},
    }),
    mergeSessions: async () => ({ state: "completed" }),
    getFiles: async () => [],
    getPlots: async () => ({ partial: false, plots: [], warnings: [] }),
    editPlotPoint: async () => ({
      partial: false,
      plots: [],
      warnings: [],
      editable: true,
    }),
    getContributionDraft: async () => ({
      eligible: false,
      reason: "No profile output",
      repository: "bramstroker/homeassistant-powercalc",
      base_branch: "master",
      manufacturer_name: "",
      manufacturer_directory: "",
      model_id: "",
      product_name: "",
      contributor: "",
      device_info: {},
      home_assistant: {},
      notes: "",
      files: [],
      commit_message: "",
      pr_title: "",
      pr_body: "",
      branch_name: "",
      warnings: [],
    }),
    previewContribution: async (_sessionId, request) => ({
      eligible: true,
      repository: "bramstroker/homeassistant-powercalc",
      base_branch: "master",
      manufacturer_name: request.manufacturer_name,
      manufacturer_directory: "signify",
      model_id: request.model_id,
      product_name: request.product_name,
      contributor: request.contributor,
      device_info: {},
      home_assistant: {},
      notes: request.notes,
      files: [],
      commit_message: "Add measured profile",
      pr_title: "Add measured profile",
      pr_body: "",
      branch_name: "measure/profile",
      warnings: [],
    }),
    submitContribution: async () => ({
      status: "success",
      pull_request_url: "https://github.com/pull/1",
    }),
    ...overrides,
  };
}

describe("measure app controller", () => {
  it.each(["snapshot", "request"] as const)(
    "prevents profile navigation for average measurements from %s",
    (source) => {
      const appState = state();
      const request = {
        measure_type: "average" as const,
        duration: 60,
        model_id: "",
        product_name: "",
        measure_device: "Test meter",
        generate_model: false,
        parameters: capabilities.defaults,
        resume_policy: "new" as const,
        power_meter: { type: "dummy" as const },
      };
      appState.view = "result";
      appState.snapshot = {
        state: "completed",
        ...(source === "snapshot" ? { request } : {}),
      };
      if (source === "request") appState.request = request;
      const controller = new MeasureAppController(
        appState,
        () => api(),
        () => connection(),
        () => undefined,
      );
      controller.openProfile();
      expect(appState.view).toBe("result");
      controller.openSubmit();
      expect(appState.view).toBe("result");
      controller.backToProfile();
      expect(appState.view).toBe("result");
    },
  );

  it("preserves structured help from API errors", async () => {
    const appState = state();
    const help = {
      url: "https://docs.powercalc.nl/contributing/measure/low-power-measurements/",
      label: "Low-power measurement guide",
    };
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          preflight: async () => {
            throw new ApiError("The meter repeatedly returned 0 W.", 422, "preflight_failed", null, help);
          },
        }),
      () => connection(),
      () => undefined,
    );

    await controller.preflight({
      measure_type: "average",
      duration: 1,
      model_id: "",
      product_name: "",
      measure_device: "",
      generate_model: false,
      parameters: capabilities.defaults,
      resume_policy: "new",
      power_meter: { type: "dummy" },
    });

    expect(appState.errorMessage).toBe("The meter repeatedly returned 0 W.");
    expect(appState.errorHelp).toEqual(help);

    controller.backToSetup();
    expect(appState.errorHelp).toBeUndefined();
  });

  it("loads the matching dummy-load calibration during boot", async () => {
    const appState = state();
    const calibration = {
      description: "60 W incandescent bulb",
      resistance: 882.4,
      calibrated_at: "2026-07-16T10:00:00Z",
      power_meter_fingerprint: "hass:sensor.plug_power:sensor.plug_voltage",
    };
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getDummyLoadCalibration: async () => calibration,
        }),
      () => connection(),
      () => undefined,
    );

    await controller.boot();

    expect(appState.dummyLoadCalibration).toEqual(calibration);
  });

  it("surfaces calibration lookup failures without blocking boot", async () => {
    const appState = state();
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getDummyLoadCalibration: async () => {
            throw new Error("Calibration API unavailable");
          },
        }),
      () => connection(),
      () => undefined,
    );

    await controller.boot();

    expect(appState.view).toBe("sessions");
    expect(appState.dummyLoadCalibration).toBeNull();
    expect(appState.dummyLoadCalibrationError).toContain("Calibration API unavailable");
  });

  it("loads files and plots for a persisted terminal session", async () => {
    const appState = state();
    let calibrationCalls = 0;
    const plots = {
      partial: true,
      warnings: ["Partial data"],
      plots: [
        {
          id: "brightness",
          title: "Brightness",
          kind: "scatter" as const,
          x_label: "Brightness",
          y_label: "Power (W)",
          source: "LCT010/brightness.csv",
          series: [
            {
              label: null,
              color: "#5488e8",
              points: [{ x: 1, y: 0.5, color: null }],
            },
          ],
        },
      ],
    };
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => ({
            state: "cancelled",
            session_id: "session-1",
          }),
          getFiles: async () => [{ name: "brightness.csv", size: 10, media_type: "text/csv" }],
          getPlots: async () => plots,
          getDummyLoadCalibration: async () => {
            calibrationCalls += 1;
            return calibrationCalls === 1
              ? null
              : {
                  description: "Calibrated load",
                  resistance: 880,
                  calibrated_at: "2026-07-16T10:00:00Z",
                };
          },
        }),
      () => connection(),
      () => undefined,
    );

    await controller.boot();
    await controller.openSession("session-1");

    expect(appState.view).toBe("result");
    expect(appState.files).toHaveLength(1);
    expect(appState.plotCollection).toEqual(plots);
    expect(appState.dummyLoadCalibration?.description).toBe("Calibrated load");
  });

  it("pushes a session view onto history so Back returns to the session list", async () => {
    const appState = state();
    const stack: HistoryLocation[] = [];
    const history: AppHistory = {
      pushState(data) {
        stack.push(data);
      },
      replaceState(data) {
        stack[0] = data;
      },
      getHash: () => "",
      back() {
        stack.pop();
        return true;
      },
      listen: () => () => undefined,
    };
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => ({
            state: "completed",
            session_id: "session-1",
          }),
          getFiles: async () => [],
          getPlots: async () => ({ partial: false, plots: [], warnings: [] }),
        }),
      () => connection(),
      () => undefined,
      history,
    );

    await controller.boot();
    await controller.openSession("session-1");

    expect(stack.map((entry) => entry.view)).toEqual(["sessions", "result"]);
    expect(stack.at(-1)?.sessionId).toBe("session-1");
    controller.dispose();
  });

  it("applies a plot-point action and replaces the plot collection", async () => {
    const appState = state();
    appState.snapshot = { state: "completed", session_id: "session-1" };
    const updated = { partial: false, plots: [], warnings: [], editable: true };
    let received: unknown;
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          editPlotPoint: async (_sessionId, detail) => {
            received = detail;
            return updated;
          },
        }),
      () => connection(),
      () => undefined,
    );

    await controller.applyPlotPointAction({
      pointId: "brightness:255",
      action: "ignore",
    });

    expect(received).toEqual({ pointId: "brightness:255", action: "ignore" });
    expect(appState.plotCollection).toEqual(updated);
    expect(appState.busy).toBe(false);
  });

  it("loads contribution auth and draft data for a completed session", async () => {
    const appState = state();
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => ({
            state: "completed",
            session_id: "session-1",
          }),
          getContributionAuth: async () => ({
            connected: true,
            identity: { login: "octocat" },
            method: "device",
          }),
          getManufacturers: async () => ({
            manufacturers: ["IKEA", "Signify"],
          }),
          getMeasureDevices: async () => ({
            devices: ["Shelly Plug S", "Kasa EP25"],
          }),
          getDeviceSpecifications: async () => ({
            device_types: {
              light: [
                {
                  name: "rated_power",
                  label: "Rated power",
                  description: "Rated watts",
                  value_type: "number",
                  collection: "scalar",
                  options: [],
                },
              ],
            },
          }),
          getContributionDraft: async () => ({
            eligible: true,
            repository: "bramstroker/homeassistant-powercalc",
            base_branch: "master",
            manufacturer_name: "Signify",
            manufacturer_directory: "signify",
            model_id: "LCT010",
            product_name: "Hue lamp",
            contributor: "octocat",
            device_info: { device: "light.desk" },
            home_assistant: { version: "2026.7" },
            notes: "Measured from HA app",
            files: [
              {
                path: "profile_library/signify/LCT010/model.json",
                rendered_json: { name: "Hue lamp" },
              },
            ],
            model_json: { name: "Hue lamp" },
            commit_message: "Add Signify LCT010",
            pr_title: "Add Signify LCT010",
            pr_body: "Adds a measured profile.",
            branch_name: "measure/signify-lct010",
            warnings: [],
          }),
        }),
      () => connection(),
      () => undefined,
    );

    await controller.boot();
    await controller.openSession("session-1");

    expect(appState.view).toBe("result");
    expect(appState.contributionAuth?.identity?.login).toBe("octocat");
    expect(appState.contributionDraft?.files[0]?.path).toBe("profile_library/signify/LCT010/model.json");
    expect(appState.manufacturers).toEqual(["IKEA", "Signify"]);
    expect(appState.measureDevices).toEqual(["Shelly Plug S", "Kasa EP25"]);
    expect(appState.deviceSpecificationFields.light?.[0]?.name).toBe("rated_power");
    expect(appState.contributionPreview).toBeUndefined();
  });

  it("restores a persisted submitted contribution for the current session", async () => {
    const appState = state();
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => ({
            state: "completed",
            session_id: "session-1",
          }),
          getContributionStatus: async () => ({
            state: "submitted" as const,
            session_id: "session-1",
            submission_url: "https://github.com/pull/9",
            message: "Contribution submitted",
          }),
        }),
      () => connection(),
      () => undefined,
    );

    await controller.boot();
    await controller.openSession("session-1");

    expect(appState.contributionResult?.pull_request_url).toBe("https://github.com/pull/9");
    expect(appState.contributionResult?.status).toBe("success");
  });

  it("ignores persisted contribution status from another session", async () => {
    const appState = state();
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => ({
            state: "completed",
            session_id: "session-2",
          }),
          getContributionStatus: async () => ({
            state: "failed" as const,
            session_id: "session-1",
            error: "Old failure",
          }),
        }),
      () => connection(),
      () => undefined,
    );

    await controller.boot();
    await controller.openSession("session-2");

    expect(appState.contributionResult).toBeUndefined();
    expect(appState.contributionError).toBe("");
  });

  it("runs device login, token fallback, disconnect, preview, and submit contribution actions", async () => {
    vi.useFakeTimers();
    const appState = state();
    let devicePolls = 0;
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getContributionDeviceAuth: async () => {
            devicePolls += 1;
            return {
              status: "authorized",
              auth: {
                connected: true,
                identity: { login: "octocat" },
                method: "device",
              },
            };
          },
          previewContribution: async (_sessionId, request) => ({
            eligible: true,
            repository: "bramstroker/homeassistant-powercalc",
            base_branch: "master",
            manufacturer_name: request.manufacturer_name,
            manufacturer_directory: "signify",
            model_id: request.model_id,
            product_name: request.product_name,
            contributor: request.contributor,
            device_info: {},
            home_assistant: {},
            notes: request.notes,
            files: [
              {
                path: "profile_library/signify/LCT010/model.json",
                content: "{}",
              },
            ],
            model_json: {},
            commit_message: "Add Signify LCT010",
            pr_title: "Add Signify LCT010",
            pr_body: "",
            branch_name: "measure/signify-lct010",
            warnings: [],
          }),
        }),
      () => connection(),
      () => undefined,
    );
    appState.snapshot = { state: "completed", session_id: "session-1" };

    await controller.startContributionDeviceAuth();
    expect(appState.contributionDeviceFlow?.flow_id).toBe("flow-1");
    expect(appState.contributionDeviceStatus?.status).toBe("pending");
    expect(devicePolls).toBe(0);

    await vi.advanceTimersByTimeAsync(4_999);
    expect(devicePolls).toBe(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(appState.contributionAuth?.identity?.login).toBe("octocat");
    expect(appState.contributionDeviceFlow).toBeUndefined();

    await controller.saveContributionToken("token");
    expect(appState.contributionAuth?.method).toBe("token");

    await controller.previewContribution({
      manufacturer_name: "Signify",
      model_id: "LCT010",
      product_name: "Hue lamp",
      contributor: "octocat",
      notes: "No aliases.",
    });
    expect(appState.contributionPreview?.notes).toBe("No aliases.");

    controller.openProfile();
    expect(appState.view).toBe("profile");
    controller.openSubmit();
    expect(appState.view).toBe("submit");
    controller.backToProfile();
    expect(appState.view).toBe("profile");
    controller.backToResult();
    expect(appState.view).toBe("result");

    await controller.submitContribution({
      manufacturer_name: "Signify",
      model_id: "LCT010",
      product_name: "Hue lamp",
      contributor: "octocat",
      notes: "No aliases.",
      confirmed: true,
    });
    expect(appState.contributionResult?.pull_request_url).toBe("https://github.com/pull/1");

    await controller.disconnectContributionAuth();
    expect(appState.contributionAuth?.connected).toBe(false);
    controller.dispose();
    vi.useRealTimers();
  });

  it("preserves contribution field errors for inline feedback", async () => {
    const appState = state();
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          previewContribution: async () => {
            throw new ApiError(
              "Product name must not start with the manufacturer",
              422,
              "invalid_metadata",
              "product_name",
            );
          },
        }),
      () => connection(),
      () => undefined,
    );
    appState.snapshot = { state: "completed", session_id: "session-1" };

    await controller.previewContribution({
      manufacturer_name: "Signify",
      model_id: "LCT010",
      product_name: "Signify Hue lamp",
      contributor: "octocat",
      notes: "",
    });

    expect(appState.contributionError).toBe("Product name must not start with the manufacturer");
    expect(appState.contributionErrorField).toBe("product_name");
  });

  it("backs off automatic device polling and stops when the code expires", async () => {
    vi.useFakeTimers();
    const appState = state();
    let devicePolls = 0;
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          startContributionDeviceAuth: async () => ({
            flow_id: "flow-1",
            user_code: "ABCD-EFGH",
            verification_uri: "https://github.com/login/device",
            expires_in: 16,
            interval: 5,
          }),
          getContributionDeviceAuth: async () => {
            devicePolls += 1;
            if (devicePolls === 1) return { status: "slow_down" };
            return { status: "pending" };
          },
        }),
      () => connection(),
      () => undefined,
    );

    await controller.startContributionDeviceAuth();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(devicePolls).toBe(1);

    await vi.advanceTimersByTimeAsync(9_999);
    expect(devicePolls).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(devicePolls).toBe(2);

    await vi.advanceTimersByTimeAsync(1_000);
    expect(appState.contributionDeviceStatus?.status).toBe("expired");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(devicePolls).toBe(2);

    controller.dispose();
    vi.useRealTimers();
  });

  it("uses GitHub's retry interval and cancels polling on disposal", async () => {
    vi.useFakeTimers();
    const appState = state();
    let devicePolls = 0;
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getContributionDeviceAuth: async () => {
            devicePolls += 1;
            return { status: "slow_down", retry_after: 12 };
          },
        }),
      () => connection(),
      () => undefined,
    );

    await controller.startContributionDeviceAuth();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(devicePolls).toBe(1);
    await vi.advanceTimersByTimeAsync(11_999);
    expect(devicePolls).toBe(1);

    controller.dispose();
    await vi.advanceTimersByTimeAsync(1);
    expect(devicePolls).toBe(1);
    vi.useRealTimers();
  });

  it("boots core data and lazily fetches entities for the selected measurement", async () => {
    const requestedDomains: string[] = [];
    let catalogCalls = 0;
    let deviceClassCalls = 0;
    const appState = state();
    const appApi = api({
      getEntityCatalog: async () => {
        catalogCalls += 1;
        return {
          lights: [{ entity_id: "light.desk", name: "Desk" }],
          powers: [{ entity_id: "sensor.plug_power", name: "Plug power" }],
          voltages: [{ entity_id: "sensor.plug_voltage", name: "Plug voltage" }],
        };
      },
      getMeasureDefinitions: async () => [
        {
          measure_type: "fan",
          icon: "🌀",
          model_id_example: "WSP002",
          product_name_example: "",
          parameters: [
            {
              name: "sleep_time",
              label: "Reading interval (seconds)",
              hint: "Delay between repeated power readings and retries.",
              step: "0.1",
              group: "Sampling",
            },
          ],
          label: "Fan",
          description: "Measure fan power.",
          supports_profile: true,
          supports_resume: false,
          fields: [
            {
              name: "fan_entity_id",
              role: "controller",
              label: "Fan",
              control: "entity",
              required: true,
              entity_domains: ["fan"],
              options: [],
            },
          ],
        },
      ],
      getEntitiesByDomain: async (domain) => {
        requestedDomains.push(domain);
        return [{ entity_id: "fan.bedroom", name: "Bedroom fan" }];
      },
      getEntitiesByDeviceClass: async () => {
        deviceClassCalls += 1;
        return [];
      },
    });
    const controller = new MeasureAppController(
      appState,
      () => appApi,
      () => connection(),
      () => undefined,
    );

    await controller.boot();
    expect(appState.view).toBe("sessions");
    expect(catalogCalls).toBe(1);
    expect(appState.lights[0]?.entity_id).toBe("light.desk");
    expect(appState.powers[0]?.entity_id).toBe("sensor.plug_power");
    expect(appState.voltages[0]?.entity_id).toBe("sensor.plug_voltage");
    expect(requestedDomains).toEqual([]);
    expect(deviceClassCalls).toBe(0);

    controller.selectMeasureType("fan");
    expect(appState.selectedMeasureType).toBe("fan");
    await vi.waitFor(() => expect(appState.deviceEntities.fan?.[0]?.entity_id).toBe("fan.bedroom"));
    expect(requestedDomains).toEqual(["fan"]);
  });

  it("loads the complete entity catalog for a recorder definition that requests it", async () => {
    let allCalls = 0;
    const appState = state();
    const appApi = api({
      getMeasureDefinitions: async () => [
        {
          measure_type: "recorder",
          icon: "⏺",
          model_id_example: "",
          product_name_example: "",
          parameters: [],
          label: "Recorder",
          description: "Record entity states.",
          supports_profile: false,
          supports_resume: false,
          fields: [
            {
              name: "tracked_entity_ids",
              role: "attribute",
              label: "Tracked entities",
              control: "entity",
              required: true,
              multiple: true,
              all_entities: true,
              options: [],
            },
          ],
        },
      ],
      getAllEntities: async () => {
        allCalls += 1;
        return [{ entity_id: "climate.room", name: "Room", domain: "climate" }];
      },
    });
    const controller = new MeasureAppController(
      appState,
      () => appApi,
      () => connection(),
      () => undefined,
    );

    await controller.boot();
    controller.selectMeasureType("recorder");

    await vi.waitFor(() => expect(appState.deviceEntities["*"]?.[0]?.entity_id).toBe("climate.room"));
    expect(allCalls).toBe(1);
  });

  it("loads the all-entity catalog a duplicated session's own purpose makes visible", async () => {
    let allCalls = 0;
    const appState = state();
    const appApi = api({
      getMeasureDefinitions: async () => [
        {
          measure_type: "recorder",
          icon: "⏺",
          model_id_example: "",
          product_name_example: "",
          parameters: [],
          label: "Recorder",
          description: "Record entity states.",
          supports_profile: false,
          supports_resume: false,
          fields: [
            {
              name: "recorder_purpose",
              role: "attribute",
              label: "Purpose",
              control: "select",
              required: true,
              default: "playbook",
              options: [
                { value: "playbook", label: "Playbook" },
                { value: "complex_profile", label: "Complex profile" },
              ],
            },
            {
              name: "tracked_entity_ids",
              role: "attribute",
              label: "Tracked entities",
              control: "entity",
              required: true,
              multiple: true,
              all_entities: true,
              options: [],
              visible_when: { recorder_purpose: ["complex_profile"] },
            },
          ],
        },
      ],
      getSession: async () =>
        ({
          state: "completed",
          session_id: "session-1",
          request: {
            measure_type: "recorder",
            recorder_purpose: "complex_profile",
            tracked_entity_ids: ["climate.room"],
          },
        }) as never,
      getAllEntities: async () => {
        allCalls += 1;
        return [{ entity_id: "climate.room", name: "Room", domain: "climate" }];
      },
    });
    const controller = new MeasureAppController(
      appState,
      () => appApi,
      () => connection(),
      () => undefined,
    );

    await controller.boot();
    await controller.duplicateSession("session-1");

    // The type's own default purpose hides the field; only the stored request reveals it.
    expect(allCalls).toBe(1);
    expect(appState.deviceEntities["*"]?.[0]?.entity_id).toBe("climate.room");
  });

  it("fills the running-view log from the session log endpoint", async () => {
    const appState = state();
    const lines = [
      { time: "2026-09-13T14:00:00Z", message: "Meters: 1.58 W" },
      { time: "2026-09-13T14:00:01Z", message: "Starting HS sweep" },
    ];
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSessions: async () => [sessionSummary({ state: "running", active: true, percent: 50 })],
          getSession: async () => ({
            state: "running",
            session_id: "session-1",
          }),
          getSessionLogs: async () => lines,
        }),
      () => connection(),
      () => undefined,
    );

    await controller.boot();
    await vi.waitFor(() => expect(appState.logs).toEqual(lines));
    expect(appState.view).toBe("running");
  });

  it("merges live log lines by sequence so SSE and the poll do not duplicate", () => {
    const first = { time: "t1", message: "Meters: 1.41 W", sequence: 10 };
    const again = { time: "t1", message: "Meters: 1.41 W", sequence: 10 };
    const next = { time: "t2", message: "Meters: 1.42 W", sequence: 11 };
    expect(mergeLogEntries([first], [again, next])).toEqual([first, next]);
  });

  it("retains entity discovery errors and updates session state from the event port", async () => {
    let onEvent: ((event: SessionEvent) => void) | undefined;
    const appState = state();
    const appApi = api({
      getSessions: async () => [sessionSummary({ state: "running", active: true, percent: 50 })],
      getSession: async () => ({ state: "running", session_id: "session-1" }),
      getMeasureDefinitions: async () => [
        {
          measure_type: "fan",
          icon: "🌀",
          model_id_example: "WSP002",
          product_name_example: "",
          parameters: [
            {
              name: "sleep_time",
              label: "Reading interval (seconds)",
              hint: "Delay between repeated power readings and retries.",
              step: "0.1",
              group: "Sampling",
            },
          ],
          label: "Fan",
          description: "Measure fan power.",
          supports_profile: true,
          supports_resume: false,
          fields: [
            {
              name: "fan_entity_id",
              role: "controller",
              label: "Fan",
              control: "entity",
              required: true,
              entity_domains: ["fan"],
              options: [],
            },
          ],
        },
      ],
      getEntitiesByDomain: async (domain) => {
        if (domain === "fan") throw new Error("Entity API failed");
        return [];
      },
    });
    const controller = new MeasureAppController(
      appState,
      () => appApi,
      (_sessionId, callbacks) => {
        onEvent = callbacks.onEvent;
        return connection();
      },
      () => undefined,
    );

    await controller.boot();
    controller.selectMeasureType("fan");
    await vi.waitFor(() => expect(appState.deviceEntityErrors.fan).toBe("Entity API failed"));

    onEvent?.({
      sequence: 1,
      type: "sample",
      created_at: "2026-09-06T12:00:00Z",
      data: { power: 12.5 },
      snapshot: { state: "running" },
    });
    expect(appState.samples).toEqual([{ power: 12.5, at: "2026-09-06T12:00:00Z" }]);

    const warning = "Discarding measurement: 0 watt was read from the power meter";
    onEvent?.({
      sequence: 2,
      type: "warning",
      created_at: "2026-09-06T12:00:01Z",
      data: { message: warning },
      snapshot: { state: "running", warnings: [warning] },
    });
    expect(appState.logs).toEqual([{ time: "2026-09-06T12:00:01Z", message: warning, sequence: 2 }]);
  });

  it("reloads effective capabilities after saving measurement defaults", async () => {
    const appState = state();
    let currentCapabilities = capabilities;
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getCapabilities: async () => currentCapabilities,
          saveSettings: async (value) => {
            currentCapabilities = {
              ...capabilities,
              defaults: {
                ...capabilities.defaults,
                ...value.measurement_defaults,
              },
            };
            return value;
          },
        }),
      () => connection(),
      () => undefined,
    );
    await controller.boot();
    controller.openSettings();
    const updated = {
      ...settings,
      measurement_defaults: {
        ...measurementDefaults,
        sleep_time: 4,
        sample_count: 3,
      },
    };

    await controller.saveSettings(updated);

    expect(appState.capabilities?.defaults.sleep_time).toBe(4);
    expect(appState.capabilities?.defaults.sample_count).toBe(3);
    expect(appState.view).toBe("sessions");
  });

  it("reloads the matching dummy-load calibration after changing the power meter", async () => {
    const appState = state();
    let calibrationCalls = 0;
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getDummyLoadCalibration: async () => {
            calibrationCalls += 1;
            return calibrationCalls === 1
              ? null
              : {
                  description: "Heater",
                  resistance: 1200,
                  calibrated_at: "2026-07-16T11:00:00Z",
                };
          },
        }),
      () => connection(),
      () => undefined,
    );
    await controller.boot();
    controller.openSettings();

    await controller.saveSettings({
      ...settings,
      power_meter: "shelly",
      shelly_ip: "192.0.2.10",
    });

    expect(calibrationCalls).toBe(2);
    expect(appState.dummyLoadCalibration?.description).toBe("Heater");
  });

  it("retains the previous calibration and can retry after a refresh failure", async () => {
    const appState = state();
    const calibration = {
      description: "Heater",
      resistance: 1200,
      calibrated_at: "2026-07-16T11:00:00Z",
    };
    let calibrationResult: "success" | "failure" = "success";
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getDummyLoadCalibration: async () => {
            if (calibrationResult === "failure") throw new Error("Calibration API unavailable");
            return calibration;
          },
        }),
      () => connection(),
      () => undefined,
    );
    await controller.boot();
    controller.openSettings();
    calibrationResult = "failure";

    await controller.saveSettings(settings);

    expect(appState.dummyLoadCalibration).toEqual(calibration);
    expect(appState.dummyLoadCalibrationError).toContain("Calibration API unavailable");

    calibrationResult = "success";
    await controller.retryDummyLoadCalibration();

    expect(appState.dummyLoadCalibrationError).toBe("");
    expect(appState.dummyLoadCalibration).toEqual(calibration);
  });

  it("ignores a validation result after the meter configuration changes", async () => {
    let resolveValidation: (result: PowerMeterDiagnostic) => void = () => undefined;
    const validation = new Promise<PowerMeterDiagnostic>((resolve) => {
      resolveValidation = resolve;
    });
    const appState = state();
    const controller = new MeasureAppController(
      appState,
      () => api({ testPowerMeter: async () => validation }),
      () => connection(),
      () => undefined,
    );

    const pending = controller.testPowerMeter(settings);
    controller.clearPowerMeterTestResult();
    resolveValidation({
      success: true,
      power: 2.3,
      status: "good",
      precision_decimals: 1,
      max_report_interval_seconds: 1,
      reports_observed: 10,
      duration_seconds: 12,
      precision_status: "good",
      update_interval_status: "good",
      messages: [],
    });
    await pending;

    expect(appState.testingPowerMeter).toBe(false);
    expect(appState.powerMeterTestResult).toBeUndefined();
  });

  it("records the requested settings section so the GitHub shortcut lands on the GitHub tab", () => {
    const appState = state();
    appState.view = "result";
    const controller = new MeasureAppController(
      appState,
      () => api(),
      () => connection(),
      () => undefined,
    );

    controller.openSettings("github");

    expect(appState.view).toBe("settings");
    expect(appState.settingsSection).toBe("github");
  });

  it("clears the requested settings section when opening settings without a target", () => {
    const appState = state();
    appState.view = "result";
    appState.settingsSection = "github";
    const controller = new MeasureAppController(
      appState,
      () => api(),
      () => connection(),
      () => undefined,
    );

    controller.openSettings();

    expect(appState.settingsSection).toBeUndefined();
  });

  it("loads canonical measurement-device names without blocking settings", async () => {
    const appState = state();
    appState.view = "setup";
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getMeasureDevices: async () => ({
            devices: ["Shelly Plug S", "TP-Link Kasa KP115"],
          }),
        }),
      () => connection(),
      () => undefined,
    );

    controller.openSettings();

    expect(appState.view).toBe("settings");
    expect(appState.measureDevicesLoading).toBe(true);
    await vi.waitFor(() => expect(appState.measureDevicesLoading).toBe(false));
    expect(appState.measureDevices).toEqual(["Shelly Plug S", "TP-Link Kasa KP115"]);
    expect(appState.measureDevicesError).toBe("");
  });

  it("keeps settings usable when measurement-device suggestions fail", async () => {
    const appState = state();
    appState.view = "setup";
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getMeasureDevices: async () => {
            throw new Error("Library unavailable");
          },
        }),
      () => connection(),
      () => undefined,
    );

    controller.openSettings();

    await vi.waitFor(() => expect(appState.measureDevicesLoading).toBe(false));
    expect(appState.view).toBe("settings");
    expect(appState.measureDevices).toEqual([]);
    expect(appState.measureDevicesError).toBe("Library unavailable");
  });

  it("discovers Shellys when opening Shelly settings and exposes unavailable discovery", async () => {
    const appState = state();
    appState.view = "setup";
    appState.settings = {
      ...settings,
      power_meter: "shelly",
      shelly_ip: "10.0.0.5",
    };
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getShellyDevices: async () => ({
            available: false,
            message: "Shelly discovery requires Home Assistant 2025.5 or newer.",
            devices: [
              {
                id: "shellyplug-s-aabbcc",
                name: "Shelly Plug S",
                model: "SHPLG-S",
                generation: 1,
                ip_address: "10.0.0.5",
                supported: true,
                reason: null,
                auth_required: false,
              },
            ],
          }),
        }),
      () => connection(),
      () => undefined,
    );

    controller.openSettings();

    expect(appState.view).toBe("settings");
    expect(appState.discoveringShellys).toBe(true);
    await vi.waitFor(() => expect(appState.discoveringShellys).toBe(false));
    expect(appState.shellyDiscoveryDevices).toHaveLength(1);
    expect(appState.shellyDiscoveryAvailable).toBe(false);
    expect(appState.shellyDiscoveryMessage).toContain("2025.5");
  });

  it("ignores a stale Shelly discovery result after refresh", async () => {
    let resolveFirst: (value: Awaited<ReturnType<MeasureAppApi["getShellyDevices"]>>) => void = () => undefined;
    const first = new Promise<Awaited<ReturnType<MeasureAppApi["getShellyDevices"]>>>((resolve) => {
      resolveFirst = resolve;
    });
    let calls = 0;
    const appState = state();
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getShellyDevices: async () => {
            calls += 1;
            if (calls === 1) return first;
            return {
              available: true,
              message: null,
              devices: [
                {
                  id: "new",
                  name: "New Shelly",
                  model: null,
                  generation: 2,
                  ip_address: "10.0.0.8",
                  supported: true,
                  reason: null,
                  auth_required: false,
                },
              ],
            };
          },
        }),
      () => connection(),
      () => undefined,
    );

    const stale = controller.discoverShellys();
    await controller.discoverShellys();
    resolveFirst({
      available: true,
      message: null,
      devices: [
        {
          id: "old",
          name: "Old Shelly",
          model: null,
          generation: 1,
          ip_address: "10.0.0.7",
          supported: true,
          reason: null,
          auth_required: false,
        },
      ],
    });
    await stale;

    expect(appState.shellyDiscoveryDevices.map((device) => device.id)).toEqual(["new"]);
  });

  it("starts a camera preview when a new measurement uses an OCR meter, so it is visible on setup before preflight", async () => {
    const appState = state();
    appState.settings = {
      ...settings,
      power_meter: "ocr",
      ocr_source: "http://camera.local/",
    };
    const startMeterPreview = vi.fn(async () => ({
      preview_id: "preview-1",
      labels: ["primary"],
    }));
    const controller = new MeasureAppController(
      appState,
      () => api({ startMeterPreview }),
      () => connection(),
      () => undefined,
    );

    controller.newMeasurement();
    await vi.waitFor(() => {
      expect(appState.meterPreviewId).toBe("preview-1");
    });

    expect(startMeterPreview).toHaveBeenCalled();
    expect(appState.view).toBe("setup");
    expect(appState.meterPreviewLabels).toEqual(["primary"]);
  });

  it("keeps the camera preview running when leaving Defaults back to setup", async () => {
    const appState = state();
    appState.settings = { ...settings, power_meter: "ocr", ocr_source: "0" };
    appState.view = "setup";
    appState.meterPreviewId = "preview-1";
    appState.meterPreviewLabels = ["primary"];
    const startMeterPreview = vi.fn(async () => ({
      preview_id: "preview-2",
      labels: ["primary"],
    }));
    const stopMeterPreview = vi.fn(async () => undefined);
    const controller = new MeasureAppController(
      appState,
      () => api({ startMeterPreview, stopMeterPreview }),
      () => connection(),
      () => undefined,
    );

    controller.openSettings();
    expect(appState.view).toBe("settings");
    controller.closeSettings();
    await Promise.resolve();

    expect(appState.view).toBe("setup");
    expect(stopMeterPreview).not.toHaveBeenCalled();
    expect(startMeterPreview).not.toHaveBeenCalled();
    expect(appState.meterPreviewId).toBe("preview-1");
  });

  it("reconnects a dead aiming preview from current Defaults without changing the setup draft", async () => {
    const appState = state();
    appState.view = "setup";
    appState.settings = { ...settings, power_meter: "ocr", ocr_source: "0" };
    appState.request = {
      measure_type: "light",
      model_id: "kept",
      product_name: "kept lamp",
      measure_device: "Desk",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["color_temp" as const],
      parameters: capabilities.defaults,
      resume_policy: "new",
      power_meter: { type: "ocr", source: "0" },
      controller: { type: "dummy" },
    };
    appState.meterPreviewId = "preview-dead";
    appState.meterPreviewLabels = ["primary"];
    const startMeterPreview = vi.fn(async () => ({
      preview_id: "preview-new",
      labels: ["primary"],
    }));
    const stopMeterPreview = vi.fn(async () => undefined);
    const controller = new MeasureAppController(
      appState,
      () => api({ startMeterPreview, stopMeterPreview }),
      () => connection(),
      () => undefined,
    );

    await controller.reconnectMeterPreview();

    expect(appState.view).toBe("setup");
    expect(appState.request?.model_id).toBe("kept");
    expect(appState.meterPreviewId).toBe("preview-new");
    expect(startMeterPreview).toHaveBeenCalled();
    expect(stopMeterPreview).toHaveBeenCalledWith("preview-dead");
  });

  it("releases the aiming preview before starting a session so the session can bind the preview port", async () => {
    const appState = state();
    appState.request = {
      measure_type: "average",
      duration: 1,
      model_id: "",
      product_name: "",
      measure_device: "",
      generate_model: false,
      parameters: capabilities.defaults,
      resume_policy: "new",
      power_meter: { type: "dummy" },
    };
    appState.meterPreviewId = "preview-1";
    const order: string[] = [];
    const stopMeterPreview = vi.fn(async () => {
      order.push("stop");
    });
    const start = vi.fn(async () => {
      order.push("start");
      return { state: "running" as const };
    });
    const controller = new MeasureAppController(
      appState,
      () => api({ stopMeterPreview, start }),
      () => connection(),
      () => undefined,
    );

    await controller.start();

    expect(order.slice(0, 2)).toEqual(["stop", "start"]);
    expect(appState.meterPreviewId).toBeNull();
  });

  it("refines a session with extend policy and the seed id", async () => {
    const appState = state();
    const request = {
      measure_type: "light" as const,
      model_id: "36871",
      product_name: "Living room GU10",
      measure_device: "Desk meter",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["color_temp" as const],
      parameters: capabilities.defaults,
      resume_policy: "new" as const,
      power_meter: { type: "dummy" as const },
      controller: { type: "dummy" as const },
    };
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => ({
            state: "completed",
            session_id: "seed-1",
            request,
          }),
        }),
      () => connection(),
      () => undefined,
    );

    await controller.refineSession("seed-1");

    expect(appState.view).toBe("setup");
    expect(appState.request).toEqual(
      expect.objectContaining({
        resume_policy: "extend",
        seed_session_id: "seed-1",
        remeasure_existing: false,
        model_id: "36871",
      }),
    );
  });

  it("starts the camera preview when refining a session that uses OCR", async () => {
    const appState = state();
    appState.settings = {
      ...settings,
      power_meter: "ocr",
      ocr_source: "http://camera.local/",
    };
    const request = {
      measure_type: "light" as const,
      model_id: "36871",
      product_name: "Living room GU10",
      measure_device: "Desk meter",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["color_temp" as const],
      parameters: capabilities.defaults,
      resume_policy: "new" as const,
      power_meter: { type: "ocr" as const, source: "http://camera.local/" },
      controller: { type: "dummy" as const },
    };
    const startMeterPreview = vi.fn(async () => ({
      preview_id: "preview-refine",
      labels: ["primary"],
    }));
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          startMeterPreview,
          getSession: async () => ({
            state: "completed",
            session_id: "seed-1",
            request,
          }),
        }),
      () => connection(),
      () => undefined,
    );

    await controller.refineSession("seed-1");

    expect(appState.view).toBe("setup");
    expect(startMeterPreview).toHaveBeenCalled();
    expect(appState.meterPreviewId).toBe("preview-refine");
  });

  it("starts the camera preview after a failed preflight so the reject reason is visible", async () => {
    const appState = state();
    appState.settings = {
      ...settings,
      power_meter: "ocr",
      ocr_source: "http://camera.local/",
    };
    appState.view = "setup";
    const startMeterPreview = vi.fn(async () => ({
      preview_id: "preview-after-fail",
      labels: ["primary"],
    }));
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          startMeterPreview,
          preflight: async () => {
            throw new Error("OCR: no accepted reading yet; last frame: voltage unreadable: ''");
          },
        }),
      () => connection(),
      () => undefined,
    );

    await controller.preflight({
      measure_type: "light",
      model_id: "x",
      product_name: "x",
      measure_device: "Desk",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["color_temp"],
      parameters: capabilities.defaults,
      resume_policy: "extend",
      seed_session_id: "seed-1",
      power_meter: { type: "ocr", source: "http://camera.local/" },
      controller: { type: "dummy" },
    });

    expect(appState.view).toBe("setup");
    expect(appState.errorMessage).toContain("voltage unreadable");
    expect(startMeterPreview).toHaveBeenCalled();
    expect(appState.meterPreviewId).toBe("preview-after-fail");
  });

  it("restores the camera preview when starting a session fails", async () => {
    const appState = state();
    appState.settings = {
      ...settings,
      power_meter: "ocr",
      ocr_source: "http://camera.local/",
    };
    appState.view = "review";
    appState.request = {
      measure_type: "light",
      model_id: "x",
      product_name: "x",
      measure_device: "Desk",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["color_temp"],
      parameters: capabilities.defaults,
      resume_policy: "new",
      power_meter: { type: "ocr", source: "http://camera.local/" },
      controller: { type: "dummy" },
    };
    appState.meterPreviewId = "preview-old";
    const startMeterPreview = vi.fn(async () => ({
      preview_id: "preview-restored",
      labels: ["primary"],
    }));
    const stopMeterPreview = vi.fn(async () => undefined);
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          startMeterPreview,
          stopMeterPreview,
          start: async () => {
            throw new Error("OCR: cannot open video source");
          },
        }),
      () => connection(),
      () => undefined,
    );

    await controller.start();

    expect(appState.view).toBe("review");
    expect(stopMeterPreview).toHaveBeenCalled();
    expect(startMeterPreview).toHaveBeenCalled();
    expect(appState.meterPreviewId).toBe("preview-restored");
  });

  it("keeps measure-again as a fresh copy without a seed", async () => {
    const appState = state();
    const request = {
      measure_type: "light" as const,
      model_id: "36871",
      product_name: "Living room GU10",
      measure_device: "Desk meter",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["color_temp" as const],
      parameters: capabilities.defaults,
      resume_policy: "extend" as const,
      seed_session_id: "old-seed",
      remeasure_existing: true,
      power_meter: { type: "dummy" as const },
      controller: { type: "dummy" as const },
    };
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => ({
            state: "completed",
            session_id: "seed-1",
            request,
          }),
        }),
      () => connection(),
      () => undefined,
    );

    await controller.duplicateSession("seed-1");

    expect(appState.request?.resume_policy).toBe("new");
    expect(appState.request?.seed_session_id).toBeUndefined();
    expect(appState.request?.remeasure_existing).toBe(false);
  });

  it("reattaches extend fields after setup rebuilds the request", async () => {
    const appState = state();
    appState.request = {
      measure_type: "light",
      model_id: "36871",
      product_name: "Living room GU10",
      measure_device: "Desk meter",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["color_temp"],
      parameters: capabilities.defaults,
      resume_policy: "extend",
      seed_session_id: "seed-1",
      remeasure_existing: true,
      power_meter: { type: "dummy" },
      controller: { type: "dummy" },
    };
    const rebuilt = {
      ...appState.request,
      resume_policy: "new" as const,
      seed_session_id: undefined,
      remeasure_existing: false,
    };
    const controller = new MeasureAppController(
      appState,
      () => api(),
      () => connection(),
      () => undefined,
    );

    await controller.preflight(rebuilt);

    expect(appState.request?.resume_policy).toBe("extend");
    expect(appState.request?.seed_session_id).toBe("seed-1");
    expect(appState.request?.remeasure_existing).toBe(true);
    expect(appState.view).toBe("running");
  });

  it("keeps the review step when preflight reports a warning", async () => {
    const appState = state();
    const start = vi.fn(async () => ({ state: "running" as const }));
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          start,
          preflight: async () => ({
            valid: true,
            warnings: ["The power sensor does not report often enough."],
          }),
        }),
      () => connection(),
      () => undefined,
    );

    await controller.preflight({
      measure_type: "light",
      model_id: "36871",
      product_name: "Living room GU10",
      measure_device: "Desk meter",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["color_temp"],
      parameters: capabilities.defaults,
      resume_policy: "new",
      power_meter: { type: "dummy" },
      controller: { type: "dummy" },
    });

    expect(appState.view).toBe("review");
    expect(start).not.toHaveBeenCalled();
  });

  it("does not take light-check watt readings during preflight", async () => {
    const appState = state();
    const probed: string[] = [];
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          preflight: async () => ({
            valid: true,
            warnings: ["needs review"],
            probe_steps: [
              { id: "standby", kind: "standby", label: "Standby" },
              {
                id: "0",
                kind: "on",
                label: "Brightness 1",
                mode: "brightness",
              },
            ],
          }),
          preflightProbe: async (_request, step) => {
            probed.push(step);
            return step === "standby"
              ? {
                  id: "standby",
                  kind: "standby",
                  label: "Standby",
                  power_w: 0.2,
                }
              : {
                  id: "0",
                  kind: "on",
                  label: "Brightness 1",
                  power_w: 1.25,
                  mode: "brightness",
                };
          },
          preflightProbeComplete: async (standby, points) => ({
            checked_variations: points.length,
            minimum_aggregate_power_w: 1.25,
            standby_aggregate_power_w: standby,
            points,
          }),
        }),
      () => connection(),
      () => undefined,
    );

    await controller.preflight({
      measure_type: "light",
      model_id: "36871",
      product_name: "Living room GU10",
      measure_device: "Desk meter",
      generate_model: true,
      gzip: false,
      multiple_light_count: 1,
      modes: ["brightness"],
      parameters: capabilities.defaults,
      resume_policy: "new",
      power_meter: { type: "hass", entity_id: "sensor.power" },
      controller: { type: "hass", entity_id: "light.test" },
    });

    expect(probed).toEqual([]);
    expect(appState.preflight?.light_load_probe).toBeUndefined();
    expect(appState.view).toBe("review");
  });

  it("previews and confirms a merge into a new result session", async () => {
    const appState = state();
    appState.snapshot = { state: "completed", session_id: "left-1" };
    const preview = {
      left: "left-1",
      right: "right-1",
      model_id_warning: false,
      modes: {
        hs: {
          kept: 10,
          added: 4,
          replaced: 1,
          replaced_reasons: { settle: 1 },
        },
      },
    };
    const previewMerge = vi.fn(async () => preview);
    const mergeSessions = vi.fn(async () => ({
      state: "completed" as const,
      session_id: "merged-1",
      request: {
        measure_type: "light" as const,
        model_id: "36867",
        product_name: "KAJPLATS",
        measure_device: "Desk meter",
        generate_model: true,
        gzip: false,
        multiple_light_count: 1,
        modes: ["hs" as const],
        parameters: capabilities.defaults,
        resume_policy: "new" as const,
        derived_from: ["left-1", "right-1"],
        power_meter: { type: "dummy" as const },
        controller: { type: "dummy" as const },
      },
    }));
    const merged = {
      state: "completed" as const,
      session_id: "merged-1",
      request: {
        measure_type: "light" as const,
        model_id: "36867",
        product_name: "KAJPLATS",
        measure_device: "Desk meter",
        generate_model: true,
        gzip: false,
        multiple_light_count: 1,
        modes: ["hs" as const],
        parameters: capabilities.defaults,
        resume_policy: "new" as const,
        derived_from: ["left-1", "right-1"],
        power_meter: { type: "dummy" as const },
        controller: { type: "dummy" as const },
      },
    };
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          previewMerge,
          mergeSessions,
          getSession: async () => merged,
        }),
      () => connection(),
      () => undefined,
    );

    await expect(controller.previewMerge("right-1")).resolves.toEqual(preview);
    expect(previewMerge).toHaveBeenCalledWith("left-1", "right-1");

    await controller.confirmMerge("right-1");
    expect(mergeSessions).toHaveBeenCalledWith("left-1", "right-1");
    expect(appState.snapshot?.session_id).toBe("merged-1");
    expect(appState.view).toBe("result");
  });

  it("leaves the running view when a snapshot poll sees the session completed", async () => {
    vi.useFakeTimers();
    const appState = state();
    let sessionState: "running" | "completed" = "running";
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => ({
            state: sessionState,
            session_id: "session-1",
            progress: { completed: 22, total: 32, percent: 69 },
          }),
        }),
      () => connection(),
      () => undefined,
    );

    try {
      await controller.openSession("session-1");
      expect(appState.view).toBe("running");

      sessionState = "completed";
      await vi.advanceTimersByTimeAsync(3_000);

      expect(appState.view).toBe("result");
    } finally {
      controller.dispose();
      vi.useRealTimers();
    }
  });

  it("reloads the session when a live run fails so the result page keeps the error", async () => {
    const appState = state();
    let session: {
      state: "running" | "failed";
      session_id: string;
      error?: string;
    } = { state: "running", session_id: "session-1" };
    let onEvent: ((event: SessionEvent) => void) | undefined;
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          getSession: async () => session,
        }),
      (_sessionId, callbacks) => {
        onEvent = callbacks.onEvent;
        return connection();
      },
      () => undefined,
    );

    await controller.openSession("session-1");
    expect(appState.view).toBe("running");

    session = {
      state: "failed",
      session_id: "session-1",
      error: "Home Assistant did not accept light.turn_on",
    };
    onEvent?.({
      sequence: 1,
      type: "state",
      created_at: "2026-09-11T00:00:00Z",
      data: {},
      snapshot: { state: "failed", session_id: "session-1" },
    });

    try {
      await vi.waitFor(() => expect(appState.view).toBe("result"));
      expect(appState.snapshot?.error).toBe("Home Assistant did not accept light.turn_on");
    } finally {
      controller.dispose();
    }
  });

  it("clears the recorded failure as soon as resume is clicked", async () => {
    const appState = state();
    appState.view = "result";
    appState.snapshot = {
      session_id: "session-1",
      state: "failed",
      error: "The meter went silent",
      warnings: ["Discarding measurement: 0 watt was read from the power meter"],
    };
    let finishResume!: (snapshot: { state: "running"; session_id: string }) => void;
    const resumeGate = new Promise<{ state: "running"; session_id: string }>((resolve) => {
      finishResume = resolve;
    });
    const controller = new MeasureAppController(
      appState,
      () =>
        api({
          resume: () => resumeGate,
        }),
      () => connection(),
      () => undefined,
    );

    const pending = controller.resume();
    await vi.waitFor(() => {
      expect(appState.snapshot?.error).toBeUndefined();
      expect(appState.snapshot?.warnings).toEqual([]);
      expect(appState.busy).toBe(true);
      expect(appState.view).toBe("result");
    });

    finishResume({ state: "running", session_id: "session-1" });
    await pending;
    expect(appState.view).toBe("running");
  });
});

function connection(): EventConnection {
  return { connect: () => undefined, close: () => undefined };
}
