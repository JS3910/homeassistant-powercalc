import {
  compareSessionRecency,
  groupSessionFamilies,
  sessionFamilyKey,
  sessionKinds,
  sessionLineageRootId,
  stackDepth,
} from "./families";
import type { SessionSummary } from "../../types";

function session(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: "session-1",
    state: "completed",
    created_at: "2026-09-11T10:00:00Z",
    updated_at: "2026-09-11T10:05:00Z",
    measure_type: "light",
    model_id: "36871",
    product_name: "IKEA of Sweden · KAJPLATS GU10 CWS 470lm",
    measure_device: "Zhurui PR10",
    completed: 40,
    total: 40,
    percent: 100,
    can_resume: false,
    file_count: 7,
    size: 2_000_000,
    active: false,
    ...overrides,
  };
}

describe("sessionFamilyKey", () => {
  it("prefers the server family key", () => {
    expect(sessionFamilyKey(session({ family_key: "light|36871|zhurui" }))).toBe(
      "light|36871|zhurui",
    );
  });

  it("falls back to type, model, and meter", () => {
    expect(sessionFamilyKey(session())).toBe("light|36871|zhurui pr10");
  });
});

describe("sessionLineageRootId", () => {
  it("walks seed_session_id back to the listed ancestor", () => {
    const oldest = session({ session_id: "oldest" });
    const older = session({ session_id: "older", seed_session_id: "oldest" });
    const newest = session({ session_id: "newest", seed_session_id: "older" });
    const byId = new Map([
      [oldest.session_id, oldest],
      [older.session_id, older],
      [newest.session_id, newest],
    ]);
    expect(sessionLineageRootId(newest, byId)).toBe("oldest");
    expect(sessionLineageRootId(oldest, byId)).toBe("oldest");
  });

  it("stops when the seed is not in the list", () => {
    const orphan = session({ session_id: "orphan", seed_session_id: "deleted" });
    expect(sessionLineageRootId(orphan, new Map([[orphan.session_id, orphan]]))).toBe("orphan");
  });
});

describe("groupSessionFamilies", () => {
  it("does not stack separate runs of the same light", () => {
    const families = groupSessionFamilies([
      session({
        session_id: "older",
        updated_at: "2026-09-11T08:00:00Z",
        modes: ["brightness"],
      }),
      session({
        session_id: "newest",
        updated_at: "2026-09-13T08:00:00Z",
        modes: ["hs", "color_temp"],
      }),
      session({
        session_id: "other-model",
        model_id: "LCT010",
        product_name: "Hue lamp",
        measure_device: "Desk meter",
      }),
    ]);

    expect(families.map((family) => family.latest.session_id)).toEqual([
      "newest",
      "other-model",
      "older",
    ]);
    expect(families.every((family) => family.earlier.length === 0)).toBe(true);
  });

  it("stacks only a refine chain behind the latest card", () => {
    const families = groupSessionFamilies([
      session({
        session_id: "oldest",
        updated_at: "2026-09-11T08:00:00Z",
        modes: ["brightness"],
      }),
      session({
        session_id: "older",
        updated_at: "2026-09-12T08:00:00Z",
        seed_session_id: "oldest",
        modes: ["color_temp"],
      }),
      session({
        session_id: "newest",
        updated_at: "2026-09-13T08:00:00Z",
        seed_session_id: "older",
        modes: ["hs", "color_temp"],
      }),
      session({
        session_id: "fresh",
        updated_at: "2026-09-13T09:00:00Z",
        modes: ["hs"],
      }),
    ]);

    expect(families).toHaveLength(2);
    expect(families[0]?.latest.session_id).toBe("fresh");
    expect(families[0]?.earlier).toEqual([]);
    expect(families[1]?.latest.session_id).toBe("newest");
    expect(families[1]?.earlier.map((item) => item.session_id)).toEqual(["older", "oldest"]);
  });

  it("surfaces an active refine ahead of its finished seed", () => {
    const families = groupSessionFamilies([
      session({
        session_id: "done",
        updated_at: "2026-09-13T12:00:00Z",
      }),
      session({
        session_id: "live",
        state: "running",
        updated_at: "2026-09-13T11:00:00Z",
        active: true,
        seed_session_id: "done",
      }),
    ]);

    expect(families).toHaveLength(1);
    expect(families[0]?.latest.session_id).toBe("live");
    expect(families[0]?.earlier[0]?.session_id).toBe("done");
  });
});

describe("stackDepth", () => {
  it("shows two layers for one hidden session and three for any larger stack", () => {
    const latest = session();
    expect(stackDepth({ latest, earlier: [] })).toBe(1);
    expect(stackDepth({ latest, earlier: [session({ session_id: "a" })] })).toBe(2);
    expect(
      stackDepth({
        latest,
        earlier: [
          session({ session_id: "a" }),
          session({ session_id: "b" }),
          session({ session_id: "c" }),
        ],
      }),
    ).toBe(3);
  });
});

describe("sessionKinds", () => {
  it("names the measured LUT modes", () => {
    expect(sessionKinds(session({ modes: ["hs", "color_temp"] }))).toBe(
      "Hue & saturation, Color temperature",
    );
    expect(sessionKinds(session({ modes: [] }))).toBe("Light");
  });
});

describe("compareSessionRecency", () => {
  it("orders by updated time when neither session is live", () => {
    const older = session({ session_id: "older", updated_at: "2026-09-11T08:00:00Z" });
    const newer = session({ session_id: "newer", updated_at: "2026-09-12T08:00:00Z" });
    expect(compareSessionRecency(newer, older)).toBeLessThan(0);
  });
});
