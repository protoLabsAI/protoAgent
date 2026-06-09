import { apiUrl } from "./api";

// Client for the server→client event bus (ADR 0003, extended ADR 0039). One EventSource
// is shared for the app's lifetime. Every event arrives as an unnamed SSE frame carrying
// `{topic, data, seq}`, so we route in JS by topic with `*`/`#` wildcard matching — a
// surface subscribes to an exact topic or a pattern. EventSource auto-reconnects and
// (because frames carry `id:`) resends Last-Event-ID, so the server replays missed events
// from its ring buffer.

type Listener = (data: Record<string, unknown>, topic: string) => void;

type Sub = { pattern: string; fn: Listener };

const subs = new Set<Sub>();
const connListeners = new Set<(connected: boolean) => void>();
let source: EventSource | null = null;
let connected = false;

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

function dispatch(raw: string) {
  let frame: { topic?: string; data?: Record<string, unknown> } = {};
  try {
    frame = JSON.parse(raw || "{}");
  } catch {
    return;
  }
  const topic = frame.topic;
  if (!topic) return;
  const data = (frame.data as Record<string, unknown>) || {};
  for (const sub of subs) {
    if (topicMatches(sub.pattern, topic)) sub.fn(data, topic);
  }
}

function ensureOpen() {
  if (source || typeof EventSource === "undefined") return;
  source = new EventSource(apiUrl("/api/events"));
  source.onopen = () => setConnected(true);
  source.onerror = () => setConnected(false); // EventSource auto-reconnects (with Last-Event-ID)
  source.onmessage = (event) => dispatch((event as MessageEvent).data);
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
