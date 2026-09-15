import type { MeasurementRequest } from "../../types";
import "./refine-banner";

const lightRequest = (overrides: Partial<MeasurementRequest> = {}): MeasurementRequest =>
  ({
    measure_type: "light",
    model_id: "36871",
    product_name: "Living room GU10",
    measure_device: "Desk meter",
    generate_model: true,
    parameters: {},
    resume_policy: "extend",
    seed_session_id: "seed-1",
    remeasure_existing: false,
    power_meter: { type: "dummy" },
    ...overrides,
  }) as MeasurementRequest;

describe("refine banner", () => {
  it("explains the extend run and carries hidden seed fields", async () => {
    const element = document.createElement("measure-refine-banner") as HTMLElement & {
      request?: MeasurementRequest;
      updateComplete: Promise<boolean>;
      shadowRoot: ShadowRoot;
    };
    element.request = lightRequest();
    document.body.append(element);
    await element.updateComplete;

    expect(element.shadowRoot.textContent).toContain("This run keeps existing points from Living room GU10");
    expect(element.shadowRoot.textContent).toContain("Only missing points will be measured.");
    expect(element.shadowRoot.querySelector<HTMLInputElement>('input[name="resume_policy"]')?.value).toBe("extend");
    expect(element.shadowRoot.querySelector<HTMLInputElement>('input[name="seed_session_id"]')?.value).toBe("seed-1");
    expect(element.shadowRoot.querySelector<HTMLInputElement>('input[name="remeasure_existing"]')?.checked).toBe(false);
  });

  it("hides itself unless the draft is an extend refine", async () => {
    const element = document.createElement("measure-refine-banner") as HTMLElement & {
      request?: MeasurementRequest;
      updateComplete: Promise<boolean>;
      shadowRoot: ShadowRoot;
    };
    element.request = lightRequest({ resume_policy: "new", seed_session_id: undefined });
    document.body.append(element);
    await element.updateComplete;
    expect(element.shadowRoot.querySelector(".banner")).toBeNull();
  });

  it("emits remesure-change from the advanced checkbox", async () => {
    const element = document.createElement("measure-refine-banner") as HTMLElement & {
      request?: MeasurementRequest;
      updateComplete: Promise<boolean>;
      shadowRoot: ShadowRoot;
    };
    element.request = lightRequest();
    document.body.append(element);
    await element.updateComplete;

    const changed = new Promise<boolean>((resolve) =>
      element.addEventListener("remeasure-change", (event) => resolve((event as CustomEvent<boolean>).detail)),
    );
    const checkbox = element.shadowRoot.querySelector<HTMLInputElement>('input[name="remeasure_existing"]')!;
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event("change"));
    await expect(changed).resolves.toBe(true);
  });
});
