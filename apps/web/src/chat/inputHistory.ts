// Terminal-style chat input history (#1496, #4928) — the last N submitted messages
// per chat session, so the composer can recall/edit/resend them with ↑/↓ like a shell
// (readline). Persisted in localStorage as bounded per-session rings; nav state (where
// you are in the ring) is per-composer and lives in ChatSessionSlot.
//
// Read `inputHistory(sessionId)` for the current ring (oldest → newest); call
// `pushInputHistory(sessionId, entry)` when a message is submitted. The in-memory cache
// avoids re-parsing localStorage on every arrow press; pushes go through here.

import { readKey, writeKey } from "../lib/storage";

const KEY = "protoagent.chat.inputHistory";
const MAX = 100; // cap each ring — plenty for recall, bounded localStorage footprint
const MAX_SESSIONS = 50;

type History = Record<string, string[]>;
let _cache: History | null = null;

function read(): History {
  const empty = (): History => Object.create(null) as History;
  try {
    const raw = readKey("local", KEY);
    if (!raw) return empty();
    const value = JSON.parse(raw) as unknown;
    // Discard the old global ring: it cannot be attributed to any one session.
    if (value === null || typeof value !== "object" || Array.isArray(value)
      || Object.getPrototypeOf(value) !== Object.prototype
      || !Object.values(value).every(Array.isArray)) return empty();
    const history = empty();
    for (const [id, ring] of Object.entries(value)) {
      history[id] = (ring as unknown[]).filter((x): x is string => typeof x === "string");
    }
    return history;
  } catch {
    return empty(); // corrupt/absent/blocked storage → empty history, never throw
  }
}

/** The session's history ring, oldest → newest. Cached after the first read. */
export function inputHistory(sessionId: string): string[] {
  if (_cache === null) _cache = read();
  return _cache[sessionId] ?? [];
}

/**
 * Record a submitted message, trimming blanks and consecutive repeats like a shell.
 * Each nonblank push refreshes the session's recency, keeping at most MAX_SESSIONS
 * rings of MAX entries. Best-effort persistence — a storage failure leaves memory intact.
 */
export function pushInputHistory(sessionId: string, entry: string): void {
  const e = entry.trim();
  if (!e) return;
  const hist = inputHistory(sessionId);
  if (hist[hist.length - 1] !== e) hist.push(e); // consecutive dedupe
  while (hist.length > MAX) hist.shift();
  const history = _cache!; // initialized by inputHistory
  delete history[sessionId];
  history[sessionId] = hist; // pushed session becomes most recently used
  const ids = Object.keys(history);
  for (const id of ids.slice(0, Math.max(0, ids.length - MAX_SESSIONS))) delete history[id];
  try {
    writeKey("local", KEY, JSON.stringify(history));
  } catch {
    /* quota / disabled storage — keep the in-memory ring, don't throw */
  }
}

/** Test-only: drop the in-memory cache so a fresh read hits localStorage again. */
export function _resetInputHistoryCache(): void {
  _cache = null;
}
