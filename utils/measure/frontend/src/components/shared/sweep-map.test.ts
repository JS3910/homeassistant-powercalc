import type { SweepCoverage } from "../../types";
import "./sweep-map";
import { hasSweepAxes, statusLabel, visibleStatus } from "./sweep-map";

describe("sweep map", () => {
  it("renders ct, hue, and sat axes from planned ticks and hides empty coverage", async () => {
    const element = document.createElement("measure-sweep-map") as HTMLElement & {
      coverage?: SweepCoverage | null;
      live: boolean;
      updateComplete: Promise<boolean>;
      shadowRoot: ShadowRoot;
    };
    document.body.append(element);
    await element.updateComplete;
    expect(element.shadowRoot.querySelector(".map")).toBeNull();

    element.coverage = {
      color_temp: [
        { value: 370, status: "done" },
        { value: 153, status: "current" },
      ],
      hue: [{ value: 0, status: "pending" }],
      saturation: [{ value: 255, status: "done" }],
    };
    element.live = true;
    await element.updateComplete;

    expect(element.shadowRoot.querySelector(".bar.ct")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".ticks")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".wheel")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".bar.sat")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".map.with-hue")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".gradients")).toBeTruthy();
    expect(element.shadowRoot.textContent).toContain("Color temp");
    expect(element.shadowRoot.textContent).toContain("Hue");
    expect(element.shadowRoot.textContent).toContain("Saturation");
    expect(element.shadowRoot.querySelectorAll(".tick")).toHaveLength(4);
    expect(element.shadowRoot.querySelector(".tick.done")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".tick.current.pulse")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".tick.pending")).toBeTruthy();
  });

  it("does not pulse the current tick on the result view", async () => {
    const element = document.createElement("measure-sweep-map") as HTMLElement & {
      coverage?: SweepCoverage;
      live: boolean;
      updateComplete: Promise<boolean>;
      shadowRoot: ShadowRoot;
    };
    element.coverage = { color_temp: [{ value: 200, status: "current" }] };
    element.live = false;
    document.body.append(element);
    await element.updateComplete;

    expect(element.shadowRoot.querySelector(".tick.current")).toBeNull();
    expect(element.shadowRoot.querySelector(".tick.partial")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".tick.pulse")).toBeNull();
  });

  it("renders inherited ticks as faded earlier-session marks", async () => {
    const element = document.createElement("measure-sweep-map") as HTMLElement & {
      coverage?: SweepCoverage;
      updateComplete: Promise<boolean>;
      shadowRoot: ShadowRoot;
    };
    element.coverage = {
      hue: [
        { value: 1, status: "inherited" },
        { value: 21845, status: "pending" },
      ],
    };
    document.body.append(element);
    await element.updateComplete;
    expect(element.shadowRoot.querySelector(".tick.inherited")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".tick.pending")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".tick.inherited")?.getAttribute("title")).toContain("earlier session");
    const red = [...element.shadowRoot.querySelectorAll<HTMLElement>(".tick")].find((tick) =>
      tick.getAttribute("aria-label")?.includes("Hue 1"),
    );
    const green = [...element.shadowRoot.querySelectorAll<HTMLElement>(".tick")].find((tick) =>
      tick.getAttribute("aria-label")?.includes("Hue 21845"),
    );
    expect(Number.parseFloat(red?.style.getPropertyValue("--hue-angle") ?? "")).toBeCloseTo(-90, 0);
    expect(Number.parseFloat(green?.style.getPropertyValue("--hue-angle") ?? "")).toBeCloseTo(30, 0);
  });

  it("renders a failed tick as a distinct status", async () => {
    const element = document.createElement("measure-sweep-map") as HTMLElement & {
      coverage?: SweepCoverage;
      updateComplete: Promise<boolean>;
      shadowRoot: ShadowRoot;
    };
    element.coverage = { color_temp: [{ value: 200, status: "failed" }] };
    document.body.append(element);
    await element.updateComplete;
    expect(element.shadowRoot.querySelector(".tick.failed")).toBeTruthy();
    expect(element.shadowRoot.querySelector(".tick.failed svg")).toBeTruthy();
  });

  it("does not invent a brightness axis", async () => {
    const element = document.createElement("measure-sweep-map") as HTMLElement & {
      coverage?: SweepCoverage;
      updateComplete: Promise<boolean>;
      shadowRoot: ShadowRoot;
    };
    element.coverage = { color_temp: [{ value: 200, status: "done" }] };
    document.body.append(element);
    await element.updateComplete;
    expect(element.shadowRoot.textContent).not.toContain("Brightness");
  });
});

describe("sweep map helpers", () => {
  it("treats empty coverage as hidden", () => {
    expect(hasSweepAxes(undefined)).toBe(false);
    expect(hasSweepAxes({})).toBe(false);
    expect(hasSweepAxes({ color_temp: [] })).toBe(false);
    expect(hasSweepAxes({ color_temp: [{ value: 1, status: "done" }] })).toBe(true);
  });

  it("downgrades current to partial when the map is not live", () => {
    expect(visibleStatus("current", true)).toBe("current");
    expect(visibleStatus("current", false)).toBe("partial");
    expect(visibleStatus("done", false)).toBe("done");
    expect(visibleStatus("failed", false)).toBe("failed");
  });

  it("labels tick states for the tooltip", () => {
    expect(statusLabel("pending")).toBe("planned");
    expect(statusLabel("current")).toBe("in progress");
  });
});
