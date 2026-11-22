// Which tab is streaming a turn (#5514).
//
// Two console tabs open on one chat share its persisted transcript. A tab that sees the
// last reply "streaming with a task id" and has no stream of its own reads that as an
// interrupted turn and reattaches to it (reattach.ts). That is right after a reload, and
// wrong while ANOTHER tab is still streaming that turn: the second tab's copy can replace
// the reply the first one finished.
//
// So the tab that holds a turn's stream leaves a short-lived marker in localStorage, one key
// per (session, task), naming the tab and the time it last renewed it. Any other tab reads
// it synchronously: a marker from another tab renewed within STALE_MS means "not yours to
// pick up yet". The marker goes away when the stream ends and on pagehide, and a tab that
// died without either stops renewing it, so it reads as gone a few heartbeats later.
//
// localStorage, not BroadcastChannel: the question has to be answered synchronously by a tab
// that has only just loaded, before any message from the owner could have arrived.
//
// Best-effort by design (lib/storage.ts): with storage unavailable or full no marker is
// written, and every tab behaves exactly as it did before markers existed.

import { listKeys, readKey, removeKey, writeKey } from "../lib/storage";

/** How often a claim renews its marker. */
export const TURN_OWNER_HEARTBEAT_MS = 2000;
/** A marker not renewed for this long is a dead tab's: three missed heartbeats. Background
 *  tabs run timers at most once a second, which stays inside it; a tab the browser throttles
 *  harder keeps its marker fresh through `renew()` on stream activity. */
export const TURN_OWNER_STALE_MS = 3 * TURN_OWNER_HEARTBEAT_MS;

const KEY_PREFIX = "protoagent.chat.turnOwner:";

type Marker = { tab: string; at: number };

/** A claim's release. Idempotent. `renew()` re-stamps the marker now; call it on stream
 *  activity so a tab whose timers the browser has throttled still reads as live. */
export type TurnClaim = (() => void) & { renew: () => void };

function newTabId(): string {
  try {
    return globalThis.crypto.randomUUID();
  } catch {
    return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
}

// Per page load, in memory. Not sessionStorage: duplicating a tab copies its sessionStorage,
// and two tabs sharing an id would each read the other's marker as their own.
let tabId = newTabId();

/** This tab's id, as written into the markers it claims. */
export function turnOwnerTabId(): string {
  return tabId;
}

/** The localStorage key holding the marker for `taskId`'s stream in `sessionId`. */
export function turnOwnerKey(sessionId: string, taskId: string): string {
  return `${KEY_PREFIX}${encodeURIComponent(sessionId)}:${encodeURIComponent(taskId)}`;
}

function parseMarker(raw: string | null): Marker | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<Marker> | null;
    if (!value || typeof value.tab !== "string" || !value.tab) return null;
    if (typeof value.at !== "number" || !Number.isFinite(value.at)) return null;
    return { tab: value.tab, at: value.at };
  } catch {
    return null; // a malformed marker vouches for nobody
  }
}

// Either direction: a marker stamped far in the future (a clock that jumped back) must not
// read as live for as long as the jump.
function isFresh(marker: Marker, now: number): boolean {
  return Math.abs(now - marker.at) < TURN_OWNER_STALE_MS;
}

/** Whether a tab OTHER than this one holds a live marker for `taskId`'s stream in
 *  `sessionId`. False with no marker, a released or stale one, an unreadable one, or this
 *  tab's own. */
export function ownedByAnotherLiveTab(sessionId: string, taskId: string): boolean {
  if (!sessionId || !taskId) return false;
  const marker = parseMarker(readKey("local", turnOwnerKey(sessionId, taskId)));
  return !!marker && marker.tab !== tabId && isFresh(marker, Date.now());
}

// ── this tab's claims ───────────────────────────────────────────────────────────────

/** Key → how many claims this tab holds on it, and the interval renewing it. */
const claims = new Map<string, { count: number; timer: ReturnType<typeof setInterval> }>();

function stamp(key: string) {
  writeKey("local", key, JSON.stringify({ tab: tabId, at: Date.now() } satisfies Marker));
}

function removeIfOurs(key: string) {
  // Another tab that claimed the turn since (it took the stream over) keeps its marker.
  if (parseMarker(readKey("local", key))?.tab === tabId) removeKey("local", key);
}

/** Drop markers nobody renews any more: a tab that crashed never released its own, and
 *  nothing else would ever remove the key. */
function sweepStale() {
  const now = Date.now();
  for (const key of listKeys("local")) {
    if (!key.startsWith(KEY_PREFIX) || claims.has(key)) continue;
    const marker = parseMarker(readKey("local", key));
    if (!marker || !isFresh(marker, now)) removeKey("local", key);
  }
}

let pagehideBound = false;

/** Release every marker when the page goes away, so a reload (or the tab closing) hands its
 *  turns over at once instead of after the stale window. The claims themselves stay: a page
 *  restored from the back/forward cache re-stamps them on its next heartbeat. */
function bindPagehide() {
  if (pagehideBound) return;
  pagehideBound = true;
  try {
    globalThis.addEventListener?.("pagehide", () => {
      for (const key of claims.keys()) removeIfOurs(key);
    });
  } catch {
    /* non-browser context */
  }
}

/** Mark this tab as the one streaming `taskId` in `sessionId`. The marker is renewed every
 *  TURN_OWNER_HEARTBEAT_MS until the returned function is called; call it when the stream
 *  ends. Claiming a turn another tab holds takes it over: the caller is the one with the
 *  stream. */
export function claimTurn(sessionId: string, taskId: string): TurnClaim {
  if (!sessionId || !taskId) return Object.assign(() => {}, { renew: () => {} });
  const key = turnOwnerKey(sessionId, taskId);
  bindPagehide();
  sweepStale();
  const held = claims.get(key);
  if (held) held.count += 1;
  else claims.set(key, { count: 1, timer: setInterval(() => stamp(key), TURN_OWNER_HEARTBEAT_MS) });
  stamp(key);

  let released = false;
  const release = () => {
    if (released) return; // a cancel and the stream's own end can both release it
    released = true;
    const entry = claims.get(key);
    if (!entry) return;
    entry.count -= 1;
    if (entry.count > 0) return; // another claim in this tab still holds the turn
    clearInterval(entry.timer);
    claims.delete(key);
    removeIfOurs(key);
  };
  const renew = () => {
    if (!released && claims.has(key)) stamp(key);
  };
  return Object.assign(release, { renew });
}

/** Call `onFree` once, as soon as no other live tab owns `taskId`'s stream in `sessionId`:
 *  its marker was released (the `storage` event says so at once) or went stale (the timer
 *  finds that). Returns a cancel. If the turn is already free, `onFree` runs on the first
 *  tick, never synchronously. */
export function whenTurnFree(sessionId: string, taskId: string, onFree: () => void): () => void {
  const key = turnOwnerKey(sessionId, taskId);
  let done = false;
  const stop = () => {
    if (done) return;
    done = true;
    clearInterval(timer);
    try {
      globalThis.removeEventListener?.("storage", onStorage);
    } catch {
      /* ignore */
    }
  };
  const check = () => {
    if (done || ownedByAnotherLiveTab(sessionId, taskId)) return;
    stop();
    onFree();
  };
  // `key === null` is a whole-storage clear, which took the marker with it.
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === key) check();
  };
  const timer = setInterval(check, TURN_OWNER_HEARTBEAT_MS);
  try {
    globalThis.addEventListener?.("storage", onStorage);
  } catch {
    /* no window — the timer alone finds it */
  }
  return stop;
}

/** Test seam: forget every claim and take a fresh tab id, as a newly loaded page would. */
export function __resetTurnOwnerForTests(): void {
  for (const [key, entry] of claims) {
    clearInterval(entry.timer);
    removeIfOurs(key);
  }
  claims.clear();
  tabId = newTabId();
}
