import { api, apiUrl } from "./api";

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

type Listener = (data: Record<string, unknown>, topic: string) => void;

type Sub = { pattern: string; fn: Listener };

const subs = new Set<Sub>();
const connListeners = new Set<(connected: boolean) => void>();
let source: EventSource | null = null;
let connected = false;
let connecting = false;
// Highest bus seq we've dispatched — replayed via `?since=` on reconnect.
let lastSeq: number | null = null;
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
  for (const sub of subs) {
    if (topicMatches(sub.pattern, topic)) sub.fn(data, topic);
  }
  return typeof frame.seq === "number" ? frame.seq : null;
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
    if (seq !== null) lastSeq = seq;
  };
}

function ensureOpen() {
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
