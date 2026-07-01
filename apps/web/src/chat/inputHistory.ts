// Terminal-style chat input history (#1496) — the last N submitted messages, so the
// composer can recall/edit/resend them with ↑/↓ like a shell (readline). Persisted in
// localStorage and shared across chat slots (one ring, like a terminal's history file);
// nav state (where you are in the ring) is per-composer and lives in ChatSurface.
//
// Read `inputHistory()` for the current ring (oldest → newest); call `pushInputHistory()`
// when a message is submitted. The in-memory cache avoids re-parsing localStorage on every
// arrow press; it stays in sync because pushes go through here.

const KEY = "protoagent.chat.inputHistory";
const MAX = 100; // cap the ring — plenty for recall, bounded localStorage footprint

let _cache: string[] | null = null;

function read(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw) as unknown;
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return []; // corrupt/absent/blocked storage → empty history, never throw
  }
}

/** The history ring, oldest → newest. Cached in memory after the first read. */
export function inputHistory(): string[] {
  if (_cache === null) _cache = read();
  return _cache;
}

/**
 * Record a submitted message as the newest history entry. No-ops on blank input and on a
 * repeat of the most-recent entry (consecutive dedupe, like a shell), so hammering the same
 * message doesn't flood the ring. Trims to the last MAX entries. Best-effort persistence —
 * a storage failure (quota / private mode) leaves the in-memory ring intact.
 */
export function pushInputHistory(entry: string): void {
  const e = entry.trim();
  if (!e) return;
  const hist = inputHistory();
  if (hist[hist.length - 1] === e) return; // consecutive dedupe
  hist.push(e);
  while (hist.length > MAX) hist.shift();
  try {
    localStorage.setItem(KEY, JSON.stringify(hist));
  } catch {
    /* quota / disabled storage — keep the in-memory ring, don't throw */
  }
}

/** Test-only: drop the in-memory cache so a fresh read hits localStorage again. */
export function _resetInputHistoryCache(): void {
  _cache = null;
}
