// Fleet ⌘K recency store. The old per-member root palette entries — quick-chat (#1733)
// and Toggle Fleet Agent (#1769) — are folded into the Fleet Room: the roster row carries
// DM / open-console / start / stop, and member names ride the room command's keywords
// (usePaletteRegistry). What remains here is the recency store the room's "Open" action
// still feeds, so a future surface can sort by last-opened.

const RECENCY_KEY = "protoagent.fleet.recent";

/** Last-opened timestamp per agent slug (localStorage). */
export function readAgentRecency(): Record<string, number> {
  try {
    const raw = localStorage.getItem(RECENCY_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? (parsed as Record<string, number>) : {};
  } catch {
    return {};
  }
}

/** Record that an agent was just opened from the palette. */
export function markAgentOpened(slug: string, now: number = Date.now()): void {
  try {
    const r = readAgentRecency();
    r[slug] = now;
    localStorage.setItem(RECENCY_KEY, JSON.stringify(r));
  } catch {
    /* localStorage unavailable — recency is a nicety, never let it block the open */
  }
}
