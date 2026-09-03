import { api, apiUrl } from "../lib/api";

// Session-attendance presence client (#3110). While a chat session is on screen the console
// holds ONE SSE stream open to `GET /api/chat/attend?session=<id>`. For the whole life of that
// stream the server records the session as attended, so a background-resume delivered into it
// (ADR 0070) becomes eligible to PARK for `ask_human` / `request_user_input` — a human is
// watching and can answer — instead of taking the unattended autonomous auto-answer. The stream
// carries no data we consume: its mere existence is the signal, and the server releases
// attendance the instant it drops (tab close, session switch, dropped socket), so presence
// fails CLOSED to unattended.
//
// Auth mirrors the event bus (lib/events.ts): a browser EventSource cannot send an
// Authorization header, so we fetch a short-lived `/api/sse-token` and pass it as `?token=`.
// Because that token expires we manage reconnection ourselves — a bearer-mode EventSource
// auto-reconnect would reuse the stale token → a permanent 401 — tearing down on error and
// reconnecting with a fresh token.

let wantSession: string | null = null; // the session the console currently wants attended
let openFor: string | null = null; // the session the live EventSource is attending (or null)
let source: EventSource | null = null;
let connecting = false;
let reconnectAttempts = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

/** Build the attendance stream URL. `session` is required; `token` is appended only when
 *  present (open-mode instances mint none). Exported for unit testing. */
export function buildAttendUrl(base: string, sessionId: string, token: string): string {
  const params = new URLSearchParams();
  params.set("session", sessionId);
  if (token) params.set("token", token);
  return `${base}${base.includes("?") ? "&" : "?"}${params.toString()}`;
}

function clearReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function teardownSource() {
  if (source) {
    source.onopen = null;
    source.onerror = null;
    source.close();
    source = null;
  }
  openFor = null;
}

function scheduleReconnect() {
  if (reconnectTimer || wantSession === null) return;
  // Exponential backoff capped at 30s so a down server (or an operator who hasn't supplied a
  // token yet) isn't hammered — matches lib/events.ts.
  const delay = Math.min(1000 * 2 ** reconnectAttempts, 30000);
  reconnectAttempts += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connect();
  }, delay);
}

async function connect() {
  if (source || connecting || typeof EventSource === "undefined") return;
  const target = wantSession;
  if (target === null) return;
  connecting = true;
  let token = "";
  try {
    token = (await api.sseToken()).token || "";
  } catch {
    // Bearer missing/invalid → request() already tripped the AuthGate. Still attempt a tokenless
    // connect: it succeeds in open mode, and in bearer mode the onerror path retries once the
    // operator supplies a token.
  }
  connecting = false;
  if (source) return; // a stream opened under us while we awaited the token
  if (wantSession !== target) {
    // The wanted session changed (or cleared) while we fetched the token — connect the new one
    // instead of stranding it (the switch that changed `wantSession` bailed early on `connecting`).
    if (wantSession !== null) void connect();
    return;
  }
  const es = new EventSource(buildAttendUrl(apiUrl("/api/chat/attend"), target, token));
  source = es;
  openFor = target;
  es.onopen = () => {
    reconnectAttempts = 0;
  };
  es.onerror = () => {
    // Drop and reconnect with a fresh token; during the gap the server's disconnect cleanup
    // releases attendance, so the session correctly reads unattended until we re-open.
    teardownSource();
    scheduleReconnect();
  };
}

/** Point attendance at `sessionId` (a blank/whitespace id or `null` stops attending). Idempotent:
 *  re-asserting the same session leaves the live stream untouched. Call with `null` on unmount so
 *  the stream is released and the session falls back to unattended. */
export function setAttendedSession(sessionId: string | null): void {
  const next = sessionId && sessionId.trim() ? sessionId : null;
  if (next === wantSession) return; // already attending (or already stopped) this exact session
  wantSession = next;
  clearReconnect();
  reconnectAttempts = 0;
  teardownSource();
  if (next !== null) void connect();
}

/** The session the live attendance stream is currently open for, or null. Exported for tests. */
export function attendedSessionForTest(): string | null {
  return openFor;
}
