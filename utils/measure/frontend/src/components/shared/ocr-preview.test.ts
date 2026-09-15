import "./ocr-preview";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }
}

type PreviewElement = HTMLElement & {
  sessionId: string;
  label: string;
  kind: "session" | "preview";
  updateComplete: Promise<boolean>;
  shadowRoot: ShadowRoot;
};

function mountPreview(): PreviewElement {
  const element = document.createElement(
    "measure-ocr-preview",
  ) as PreviewElement;
  element.kind = "preview";
  element.sessionId = "preview-1";
  element.label = "primary";
  document.body.append(element);
  return element;
}

describe("ocr preview connection", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () => new Response(JSON.stringify({ power: 1 }), { status: 200 }),
      ),
    );
  });

  afterEach(() => {
    document.body.replaceChildren();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("starts connecting without a reconnect button", async () => {
    const element = mountPreview();
    await element.updateComplete;

    expect(element.shadowRoot.textContent).toContain("Connecting…");
    expect(element.shadowRoot.querySelector("button.reconnect")).toBeNull();
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("shows Reconnect after the stream drops and emits a preview restart on click", async () => {
    const element = mountPreview();
    await element.updateComplete;
    const reconnecting = new Promise<void>((resolve) => {
      element.addEventListener("ocr-preview-reconnect", () => resolve());
    });

    FakeEventSource.instances[0]?.onerror?.();
    await element.updateComplete;

    expect(element.shadowRoot.textContent).toContain("Disconnected");
    const button = element.shadowRoot.querySelector("button.reconnect");
    expect(button).toBeTruthy();
    button!.dispatchEvent(new Event("click"));
    await reconnecting;
    expect(FakeEventSource.instances.length).toBeGreaterThan(1);
  });

  it("marks the stream live once a frame arrives", async () => {
    const element = mountPreview();
    await element.updateComplete;

    FakeEventSource.instances[0]?.onopen?.();
    FakeEventSource.instances[0]?.onmessage?.({
      data: JSON.stringify({
        version: 3,
        state: { power: 20.5, frames: 10, accepted: 9, rejected: 1, fps: 4 },
      }),
    } as MessageEvent<string>);
    await element.updateComplete;

    expect(element.shadowRoot.textContent).toContain("Live");
    expect(element.shadowRoot.textContent).toContain("20.50 W");
    expect(element.shadowRoot.querySelector("button.reconnect")).toBeNull();
  });

  it("keeps a rejection reason on its own wrapping row so the value column stays short", async () => {
    const element = mountPreview();
    element.setAttribute("beside", "");
    await element.updateComplete;
    const reason =
      "power 1.72 W disagrees with V × I × PF = 232.3 × 0.023 × 0.311 = 1.67 W";

    FakeEventSource.instances[0]?.onmessage?.({
      data: JSON.stringify({
        version: 4,
        state: {
          power: 1.72,
          last: {
            accepted: false,
            reason,
            power: 1.72,
            voltage: 232.3,
            current: 0.023,
            pf: 0.311,
            raw: {},
            timestamp: 0,
          },
        },
      }),
    } as MessageEvent<string>);
    await element.updateComplete;

    const lastValue = [...element.shadowRoot.querySelectorAll("tr")]
      .find((row) => row.textContent?.includes("Last frame"))
      ?.querySelector("td:last-child");
    expect(lastValue?.textContent?.trim()).toBe("rejected");
    expect(lastValue?.classList.contains("value")).toBe(true);
    const note = element.shadowRoot.querySelector("tr.reason td.note");
    expect(note?.textContent).toContain(reason);
    expect(note?.getAttribute("colspan")).toBe("2");
  });

  it("loads camera frames with no-store instead of cache-busting the img src", async () => {
    const fetchMock = vi.fn(
      async () => new Response(new Blob(["frame"], { type: "image/jpeg" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const element = mountPreview();
    await element.updateComplete;

    FakeEventSource.instances[0]?.onmessage?.({
      data: JSON.stringify({ version: 1, state: { power: 1 } }),
    } as MessageEvent<string>);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("frame.jpg"),
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(element.shadowRoot.querySelector("img")?.getAttribute("src") ?? "").not.toContain(
      "frame.jpg?v=",
    );

    FakeEventSource.instances[0]?.onmessage?.({
      data: JSON.stringify({ version: 2, state: { power: 2 } }),
    } as MessageEvent<string>);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("asks the parent to rebuild a reaped aiming preview on a 404 heartbeat", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => new Response("gone", { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    const element = mountPreview();
    await element.updateComplete;
    const reconnecting = new Promise<void>((resolve) => {
      element.addEventListener("ocr-preview-reconnect", () => resolve());
    });

    await vi.advanceTimersByTimeAsync(15_000);
    await reconnecting;
    expect(fetchMock).toHaveBeenCalled();
  });
});
