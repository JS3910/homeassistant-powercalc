import type { AppView } from "./app-controller";
import type { SettingsSection } from "./types";

export interface HistoryLocation {
  view: AppView;
  sessionId?: string;
  settingsSection?: SettingsSection;
}

export interface AppHistory {
  pushState(data: HistoryLocation, url: string): void;
  replaceState(data: HistoryLocation, url: string): void;
  getHash(): string;
  back(): boolean;
  listen(handler: (location: HistoryLocation) => void): () => void;
}

const SESSION_VIEWS = new Set<AppView>([
  "running",
  "result",
  "profile",
  "submit",
]);

export function hashFor(location: HistoryLocation): string {
  if (location.view === "settings") {
    return location.settingsSection
      ? `#/settings/${location.settingsSection}`
      : "#/settings";
  }
  if (SESSION_VIEWS.has(location.view) && location.sessionId) {
    return `#/${location.view}/${location.sessionId}`;
  }
  return `#/${location.view}`;
}

export function parseHash(hash: string): HistoryLocation | null {
  const path = hash.replace(/^#\/?/, "").replace(/\/+$/, "");
  if (!path) return null;
  const [rawView, extra] = path.split("/");
  const view = rawView === "share" ? "submit" : rawView;
  if (view === "settings") {
    return extra
      ? { view: "settings", settingsSection: extra as SettingsSection }
      : { view: "settings" };
  }
  if (view && SESSION_VIEWS.has(view as AppView) && extra) {
    return { view: view as AppView, sessionId: extra };
  }
  if (view === "sessions" || view === "setup" || view === "review") {
    return { view };
  }
  return null;
}

export function locationFromApp(
  view: AppView,
  sessionId?: string,
  settingsSection?: SettingsSection,
): HistoryLocation {
  if (view === "settings") return { view, settingsSection };
  if (SESSION_VIEWS.has(view) && sessionId) return { view, sessionId };
  return { view };
}

export function sameLocation(
  left: HistoryLocation,
  right: HistoryLocation,
): boolean {
  return (
    left.view === right.view &&
    left.sessionId === right.sessionId &&
    left.settingsSection === right.settingsSection
  );
}

export function historyMode(
  previous: HistoryLocation | null,
  next: HistoryLocation,
): "skip" | "replace" | "push" {
  if (next.view === "loading") return "skip";
  if (!previous) return "replace";
  if (sameLocation(previous, next)) return "skip";
  if (
    previous.sessionId &&
    previous.sessionId === next.sessionId &&
    ((previous.view === "running" && next.view === "result") ||
      (previous.view === "review" && next.view === "running"))
  ) {
    return "replace";
  }
  return "push";
}

export function silentHistory(): AppHistory {
  return {
    pushState() {},
    replaceState() {},
    getHash() {
      return "";
    },
    back() {
      return false;
    },
    listen() {
      return () => undefined;
    },
  };
}

export function createWindowHistory(): AppHistory {
  return {
    pushState(data, url) {
      history.pushState(data, "", url);
    },
    replaceState(data, url) {
      history.replaceState(data, "", url);
    },
    getHash() {
      return location.hash;
    },
    back() {
      history.back();
      return true;
    },
    listen(handler) {
      const listener = (event: PopStateEvent) => {
        const fromState = event.state as HistoryLocation | null;
        const location = fromState ?? parseHash(window.location.hash);
        if (location) handler(location);
      };
      window.addEventListener("popstate", listener);
      return () => window.removeEventListener("popstate", listener);
    },
  };
}
