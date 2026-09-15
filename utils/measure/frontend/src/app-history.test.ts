import {
  hashFor,
  historyMode,
  locationFromApp,
  parseHash,
  sameLocation,
} from "./app-history";

describe("app history", () => {
  it("round-trips session and settings hashes", () => {
    expect(parseHash("#/sessions")).toEqual({ view: "sessions" });
    expect(parseHash("#/result/abc")).toEqual({
      view: "result",
      sessionId: "abc",
    });
    expect(parseHash("#/settings/power_meter")).toEqual({
      view: "settings",
      settingsSection: "power_meter",
    });
    expect(hashFor({ view: "result", sessionId: "abc" })).toBe("#/result/abc");
    expect(hashFor({ view: "settings", settingsSection: "github" })).toBe(
      "#/settings/github",
    );
    expect(parseHash("#/share/abc")).toEqual({
      view: "submit",
      sessionId: "abc",
    });
    expect(hashFor({ view: "submit", sessionId: "abc" })).toBe("#/submit/abc");
  });

  it("replaces running-to-result for the same session so Back skips the live view", () => {
    const running = locationFromApp("running", "s1");
    const result = locationFromApp("result", "s1");
    expect(historyMode(running, result)).toBe("replace");
    expect(historyMode({ view: "sessions" }, result)).toBe("push");
    expect(historyMode(result, result)).toBe("skip");
    expect(sameLocation(running, result)).toBe(false);
  });
});
