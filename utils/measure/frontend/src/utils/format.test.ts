import {
  brightnessPercent,
  duration,
  hueDegrees,
  kelvinToMired,
  miredToKelvin,
  runningActivityLabel,
  sessionModeLabel,
  sessionStateLabel,
  snapKelvinToMired,
  stepKelvinToMired,
} from "./format";

describe("kelvin and mired conversion", () => {
  it("matches the runner's floor conversion", () => {
    expect(miredToKelvin(500)).toBe(2000);
    expect(miredToKelvin(150)).toBe(6666);
    expect(kelvinToMired(2000)).toBe(500);
    expect(kelvinToMired(6666)).toBe(150);
  });

  it("snaps Kelvin to the integer mired the runner will use", () => {
    expect(snapKelvinToMired(18001)).toEqual({ mired: 55, kelvin: 18181 });
    expect(snapKelvinToMired(18002, { min: 150, max: 500 })).toEqual({
      mired: 150,
      kelvin: 6666,
    });
    expect(brightnessPercent(224)).toBe(88);
    expect(brightnessPercent(255)).toBe(100);
    expect(hueDegrees(1)).toBe(0);
    expect(hueDegrees(21845)).toBe(120);
    expect(hueDegrees(32768)).toBe(180);
    expect(hueDegrees(65535)).toBe(360);
  });

  it("steps one mired when a 1 K nudge would otherwise snap back", () => {
    expect(snapKelvinToMired(2100)).toEqual({ mired: 476, kelvin: 2100 });
    expect(stepKelvinToMired(2099, 2100)).toEqual({ mired: 477, kelvin: 2096 });
    expect(stepKelvinToMired(2101, 2100)).toEqual({ mired: 475, kelvin: 2105 });
    expect(stepKelvinToMired(2100, 2100)).toEqual({ mired: 476, kelvin: 2100 });
    expect(stepKelvinToMired(2099, 2100, { min: 476, max: 476 })).toEqual({
      mired: 476,
      kelvin: 2100,
    });
  });
});

describe("sessionStateLabel", () => {
  it("calls a cancelled run Stopped, not Cancelled", () => {
    expect(sessionStateLabel("cancelled")).toBe("Stopped");
    expect(sessionStateLabel("cancelling")).toBe("Stopping");
    expect(sessionStateLabel("failed")).toBe("Failed");
    expect(sessionStateLabel("awaiting_confirmation")).toBe(
      "Awaiting confirmation",
    );
  });
});

describe("sessionModeLabel", () => {
  it("uses human names for LUT modes instead of the raw id", () => {
    expect(sessionModeLabel("color_temp")).toBe("Color temperature");
    expect(sessionModeLabel("hs")).toBe("Hue & saturation");
    expect(sessionModeLabel("brightness")).toBe("Brightness");
    expect(sessionModeLabel("Averaging")).toBe("Averaging");
    expect(sessionModeLabel(undefined)).toBe("—");
  });
});

describe("runningActivityLabel", () => {
  it("describes the smart stage and current HS rail instead of entity waits", () => {
    expect(
      runningActivityLabel({
        phase:
          "Waiting for light.entrance_spot_solo, light.living_room_desk to report on",
        mode: "hs",
        operating_point: {
          type: "light",
          on: true,
          brightness: 255,
          hue: 1,
          saturation: 106,
        },
      }),
    ).toBe("Hue & saturation · hue 0°, sat 42%, bri 100%");
    expect(
      runningActivityLabel({
        phase: "Discovering envelope",
        mode: "hs",
        operating_point: {
          type: "light",
          on: true,
          brightness: 255,
          hue: 1,
          saturation: 106,
        },
      }),
    ).toBe("Discovering envelope · hue 0°, sat 42%, bri 100%");
  });

  it("keeps warm-up as the activity when no operating point is set yet", () => {
    expect(
      runningActivityLabel({
        phase: "Full-brightness warm-up (1 of 2): sending command",
        mode: "hs",
      }),
    ).toBe("Full-brightness warm-up (1 of 2)");
  });

  it("does not stuff the planner reason into the activity label", () => {
    expect(
      runningActivityLabel({
        phase: "Discovering envelope",
        mode: "hs",
        operating_point: {
          type: "light",
          on: true,
          brightness: 255,
          hue: 32768,
          saturation: 3,
        },
      }),
    ).toBe("Discovering envelope · hue 180°, sat 1%, bri 100%");
  });
});

describe("duration", () => {
  it("shows zero and sub-minute values in seconds", () => {
    expect(duration(0)).toBe("0 sec");
    expect(duration(1)).toBe("1 sec");
    expect(duration(59)).toBe("59 sec");
  });

  it("shows minutes only when under an hour", () => {
    expect(duration(60)).toBe("1 min");
    expect(duration(90)).toBe("2 min");
    expect(duration(3599)).toBe("1 hr 0 min");
  });

  it("shows hours and minutes when under a day", () => {
    expect(duration(3600)).toBe("1 hr 0 min");
    expect(duration(3660)).toBe("1 hr 1 min");
    expect(duration(3 * 3600 + 10 * 60)).toBe("3 hr 10 min");
  });

  it("includes days once the duration reaches 24 hours", () => {
    expect(duration(86400)).toBe("1 day");
    expect(duration(86400 + 3600)).toBe("1 day 1 hr");
    expect(duration(86400 + 5 * 60)).toBe("1 day 5 min");
    expect(duration(2 * 86400 + 3 * 3600 + 12 * 60)).toBe("2 days 3 hr 12 min");
  });

  it("formats a multi-week HS-all estimate with leftover hours and minutes", () => {
    // 14_323_087 hr 10 min — the screenshot sample, not a realistic run.
    expect(duration(14_323_087 * 3600 + 10 * 60)).toBe(
      "596795 days 7 hr 10 min",
    );
  });
});
