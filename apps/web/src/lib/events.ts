import { api, apiUrl } from "./api";
import { agentSlug, hydrateCursor, loadCursor, rememberCursor } from "./syncCursor";

// Client for the server→client event bus (ADR 0003, extended ADR 0039). One EventSource
// is shared for the app's lifetime. Every event arrives as an unnamed SSE frame carrying
// `{topic, data, seq}`, so we route in JS by topic with `*`/`#` wildcard matching — a
// surface subscribes to an exact topic or a pattern.
//
// Auth: when a bearer is configured the server requires a short-lived HMAC token on
// `/api/events` (EventSource can't send an Authorization header). We fetch it from
// `/api/sse-token` before each connect and pass it as `?token=`. Because that token
// expires, we can't rely on EventSource's built-in reconnect (it would reuse the stale
// token → a permanent 401); instead we tear down on error and reconnect with a fresh
// token, passing `?since=<lastSeq>` so the server still replays missed events from its
// ring buffer. In open mode the token is "" and the server accepts a tokenless stream.

type Listener = (data: Record<string, unknown>, topic: string, seq?: number) => void;

type Sub = { pattern: string; fn: Listener };

/** A bus frame as retained client-side: topic + payload + the server-assigned seq. */
export type BusFrame = { topic: string; data: Record<string, unknown>; seq: number };

const subs = new Set<Sub>();
const connListeners = new Set<(connected: boolean) => void>();
let source: EventSource | null = null;
let connected = false;
let connecting = false;
// The agent whose stream this tab follows. An agent switch is a full navigation, so this is
// fixed for the module's life. Bus seqs are per-agent streams, so the resume cursor is keyed
// by it (lib/syncCursor.ts).
const AGENT_SLUG = agentSlug();

// Highest bus seq we've dispatched — replayed via `?since=` on reconnect, when the tab
// becomes visible again, and on wake from sleep. Persisted per agent by syncCursor: the
// synchronous sessionStorage mirror lets this module resume the instant it builds its first
// URL, and the durable IndexedDB copy (folded in before the first connect) survives a tab
// close so a reopened pane resumes from its cursor rather than a blind refetch. Without a
// cursor a prior tab's input-required-while-away or background completions never replayed
// even though the server's log still held them (ADR 0130 D3).
let lastSeq: number | null = loadCursor(AGENT_SLUG);
// The durable cursor is read from IndexedDB once, before the first connect (async); until
// then `lastSeq` is the synchronous mirror.
let cursorHydrated = false;
// Client-side mirror of the server's ring buffer (events/bus.py retains 128): every seq'd
// frame this console has received, so the plugin-view bridge can replay "what did I miss?"
// to an iframe that re-subscribes with `since` (#1640) WITHOUT a second server fetch. Sized
// past the server's ring so replay from here is never worse than a server-side reconnect
// for anything observed while this console was open. Best-effort like the server's: a seq
// from before this console connected (or past the horizon) yields only what's retained.
const RING_MAX = 256;
const ring: BusFrame[] = [];
// When each retained frame ARRIVED here (ms since epoch) — kept beside the frame, not on it,
// so `replaySince` hands out the frames exactly as before. Read by `seqReceivedBefore`: the
// navigate catch-up (#4283) bounds its replay to what landed just before a `ui.navigate`.
const receivedAt = new WeakMap<BusFrame, number>();
let reconnectAttempts = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

/** Topic matcher mirroring events/bus.py: `*` = one segment, `#` = tail. */
export function topicMatches(pattern: string, topic: string): boolean {
  if (pattern === "#" || pattern === topic) return true;
  const pp = pattern.split(".");
  const tp = topic.split(".");
  for (let i = 0; i < pp.length; i++) {
    if (pp[i] === "#") return true;
    if (i >= tp.length) return false;
    if (pp[i] === "*") continue;
    if (pp[i] !== tp[i]) return false;
  }
  return pp.length === tp.length;
}

function setConnected(next: boolean) {
  if (connected === next) return;
  connected = next;
  connListeners.forEach((fn) => fn(connected));
}

/** Route a raw SSE frame to matching subscribers. Returns the frame's bus seq
 *  (for `?since=` replay) or null when the frame carries none. */
function dispatch(raw: string): number | null {
  let frame: { topic?: string; data?: Record<string, unknown>; seq?: number } = {};
  try {
    frame = JSON.parse(raw || "{}");
  } catch {
    return null;
  }
  const topic = frame.topic;
  if (!topic) return null;
  const data = (frame.data as Record<string, unknown>) || {};
  const seq = typeof frame.seq === "number" ? frame.seq : undefined;
  // Retain BEFORE fanning out, so anything a listener saw live is also replayable —
  // the invariant the bridge's replay-then-live dedupe (#1640) rests on.
  if (seq !== undefined) {
    const retained: BusFrame = { topic, data, seq };
    receivedAt.set(retained, Date.now());
    ring.push(retained);
    if (ring.length > RING_MAX) ring.shift();
  }
  // Each listener in its own try (ADR 0114 D1): one throwing subscriber must not starve
  // the ones after it, nor skip the caller's `lastSeq` update.
  for (const sub of subs) {
    if (!topicMatches(sub.pattern, topic)) continue;
    try {
      sub.fn(data, topic, seq);
    } catch (err) {
      console.error(`[events] listener for ${sub.pattern} threw on ${topic}`, err);
    }
  }
  return seq ?? null;
}

/** Retained frames newer than `since`, oldest→newest — the client-side counterpart of
 *  `GET /api/events?since=` (#1640). Best-effort: only what this console has received
 *  and still retains. */
export function replaySince(since: number): BusFrame[] {
  return ring.filter((f) => f.seq > since);
}

/** The newest bus seq this console has retained (else the persisted reconnect cursor), or
 *  null before any seq'd frame — a plugin view's high-water mark when it unmounts (#4283). */
export function latestSeq(): number | null {
  return ring.length ? ring[ring.length - 1].seq : lastSeq;
}

/** The highest retained seq that ARRIVED at or before `cutoffMs` (epoch ms), or null when
 *  every retained frame is newer than that (or none is retained) — so `replaySince` of the
 *  answer is "only what landed after the cutoff" (#4283). */
export function seqReceivedBefore(cutoffMs: number): number | null {
  for (let i = ring.length - 1; i >= 0; i--) {
    const at = receivedAt.get(ring[i]);
    if (at !== undefined && at <= cutoffMs) return ring[i].seq;
  }
  return null;
}

/** Build the EventSource URL, appending `?token=` (auth) and `?since=` (replay)
 *  only when present. Exported for unit testing. */
export function buildEventsUrl(base: string, token: string, since: number | null): string {
  if (!token && since === null) return base;
  const params = new URLSearchParams();
  if (token) params.set("token", token);
  if (since !== null) params.set("since", String(since));
  return `${base}${base.includes("?") ? "&" : "?"}${params.toString()}`;
}

/** True while at least one consumer wants the stream open. */
function wanted(): boolean {
  return subs.size > 0 || connListeners.size > 0;
}

function teardownSource() {
  if (source) {
    source.onopen = null;
    source.onerror = null;
    source.onmessage = null;
    source.close();
    source = null;
  }
}

function scheduleReconnect() {
  if (reconnectTimer || !wanted()) return;
  // Exponential backoff capped at 30s so a down server (or an operator who hasn't
  // supplied a token yet) isn't hammered.
  const delay = Math.min(1000 * 2 ** reconnectAttempts, 30000);
  reconnectAttempts += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connect();
  }, delay);
}

async function connect() {
  if (source || connecting || typeof EventSource === "undefined") return;
  connecting = true;
  // Fold the durable (IndexedDB) cursor in once, before the first URL is built, so a tab
  // reopened after a close resumes from where the origin left off (ADR 0130 D3). Best-effort
  // and cheap to have missed — the sessionStorage mirror already seeded `lastSeq`.
  if (!cursorHydrated) {
    cursorHydrated = true;
    try {
      const durable = await hydrateCursor(AGENT_SLUG);
      if (durable !== null && (lastSeq === null || durable > lastSeq)) lastSeq = durable;
    } catch {
      /* durable store unavailable — keep the synchronous cursor */
    }
  }
  let token = "";
  try {
    token = (await api.sseToken()).token || "";
  } catch {
    // Bearer missing/invalid → request() already tripped the AuthGate (#873). Still
    // attempt a tokenless connect: it succeeds in open mode, and in bearer mode the
    // onerror path will retry once the operator supplies a token.
  }
  connecting = false;
  // A consumer may have torn everything down while we awaited the token.
  if (!wanted() || source) return;
  const es = new EventSource(buildEventsUrl(apiUrl("/api/events"), token, lastSeq));
  source = es;
  es.onopen = () => {
    reconnectAttempts = 0;
    setConnected(true);
  };
  es.onerror = () => {
    setConnected(false);
    teardownSource();
    scheduleReconnect();
  };
  es.onmessage = (event) => {
    const seq = dispatch((event as MessageEvent).data);
    if (seq !== null) {
      lastSeq = seq;
      rememberCursor(AGENT_SLUG, seq); // sessionStorage mirror + durable IndexedDB, best-effort
    }
  };
}

/** A tab regaining focus, becoming visible, or waking from sleep: if the stream is down and
 *  something still wants it, resume from the cursor NOW rather than waiting out the backoff —
 *  a slept laptop may never have fired the `onerror` that would have rescheduled it (ADR 0130
 *  D3). A hidden tab is left alone; it reconnects the moment it is shown. */
function reconnectOnWake() {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
  if (source || connecting || !wanted()) return;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempts = 0;
  void connect();
}

let wakeHooked = false;
function hookWake() {
  if (wakeHooked) return;
  wakeHooked = true;
  try {
    document?.addEventListener?.("visibilitychange", reconnectOnWake);
    window?.addEventListener?.("focus", reconnectOnWake);
    // iOS/desktop restore from the page cache (sleep, back-forward) fires `pageshow`, not
    // always `focus`/`visibilitychange`.
    window?.addEventListener?.("pageshow", reconnectOnWake);
  } catch {
    /* non-browser context (SSR / node test env) */
  }
}

function ensureOpen() {
  hookWake();
  void connect();
}

/** Subscribe to a topic (exact, or a `*`/`#` pattern). Returns an unsubscribe function. */
export function onTopic(pattern: string, fn: Listener): () => void {
  ensureOpen();
  const sub: Sub = { pattern, fn };
  subs.add(sub);
  return () => {
    subs.delete(sub);
  };
}

/** Back-compat alias (ADR 0003) — subscribe to an exact event name. */
export const onServerEvent = onTopic;

/** Observe connection state. Returns an unsubscribe function. */
export function onConnectionChange(fn: (connected: boolean) => void): () => void {
  ensureOpen();
  connListeners.add(fn);
  fn(connected);
  return () => {
    connListeners.delete(fn);
  };
}

export function isConnected(): boolean {
  return connected;
}
