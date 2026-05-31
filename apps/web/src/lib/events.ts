import { apiUrl } from "./api";

// Client for the server→client SSE push channel (ADR 0003). One EventSource is
// shared across the app for its lifetime; surfaces register handlers per event
// name. EventSource reconnects automatically on transient drops; we just track
// the connection state for the "live" indicator.

type Listener = (data: Record<string, unknown>) => void;

const listeners = new Map<string, Set<Listener>>();
const connListeners = new Set<(connected: boolean) => void>();
let source: EventSource | null = null;
let connected = false;

function setConnected(next: boolean) {
  if (connected === next) return;
  connected = next;
  connListeners.forEach((fn) => fn(connected));
}

function attach(name: string) {
  // Lazily bind an EventSource listener the first time a name is subscribed.
  source?.addEventListener(name, (event) => {
    let data: Record<string, unknown> = {};
    try {
      data = JSON.parse((event as MessageEvent).data || "{}");
    } catch {
      data = {};
    }
    listeners.get(name)?.forEach((fn) => fn(data));
  });
}

function ensureOpen() {
  if (source || typeof EventSource === "undefined") return;
  source = new EventSource(apiUrl("/api/events"));
  source.onopen = () => setConnected(true);
  source.onerror = () => setConnected(false); // EventSource auto-reconnects
  // Re-bind any names registered before the source existed.
  for (const name of listeners.keys()) attach(name);
}

/** Subscribe to a named server event. Returns an unsubscribe function. */
export function onServerEvent(name: string, fn: Listener): () => void {
  ensureOpen();
  let set = listeners.get(name);
  if (!set) {
    set = new Set();
    listeners.set(name, set);
    attach(name);
  }
  set.add(fn);
  return () => {
    set?.delete(fn);
  };
}

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
