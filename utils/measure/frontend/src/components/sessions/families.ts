import { humanize, sessionModeLabel } from "../../utils/format";
import type { SessionSummary } from "../../types";

export interface SessionFamily {
  latest: SessionSummary;
  earlier: SessionSummary[];
}

export function sessionFamilyKey(session: SessionSummary): string {
  if (session.family_key) return session.family_key;
  return [session.measure_type, session.model_id || session.product_name, session.measure_device]
    .map((part) => part.trim().toLowerCase())
    .join("|");
}

/** Oldest session still listed that this refine chain walks back to. */
export function sessionLineageRootId(
  session: SessionSummary,
  byId: ReadonlyMap<string, SessionSummary>,
): string {
  const seen = new Set<string>();
  let current = session;
  while (current.seed_session_id && !seen.has(current.session_id)) {
    seen.add(current.session_id);
    const parent = byId.get(current.seed_session_id);
    if (!parent) break;
    current = parent;
  }
  return current.session_id;
}

export function stackDepth(family: SessionFamily): 1 | 2 | 3 {
  const count = family.earlier.length + 1;
  if (count <= 1) return 1;
  if (count === 2) return 2;
  return 3;
}

export function compareSessionRecency(left: SessionSummary, right: SessionSummary): number {
  if (left.active !== right.active) return left.active ? -1 : 1;
  return Date.parse(right.updated_at) - Date.parse(left.updated_at);
}

export function groupSessionFamilies(sessions: SessionSummary[]): SessionFamily[] {
  const byId = new Map(sessions.map((session) => [session.session_id, session]));
  const groups = new Map<string, SessionSummary[]>();
  for (const session of sessions) {
    const key = sessionLineageRootId(session, byId);
    const list = groups.get(key);
    if (list) list.push(session);
    else groups.set(key, [session]);
  }
  return [...groups.values()]
    .map((items) => {
      const [latest, ...earlier] = [...items].sort(compareSessionRecency);
      return latest ? { latest, earlier } : undefined;
    })
    .filter((family): family is SessionFamily => family != null)
    .sort((left, right) => compareSessionRecency(left.latest, right.latest));
}

export function sessionKinds(session: SessionSummary): string {
  if (session.modes?.length) {
    return session.modes.map((mode) => sessionModeLabel(mode)).join(", ");
  }
  return humanize(session.measure_type);
}
