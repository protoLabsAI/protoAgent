// Who may hold a chat session "streaming", and the one place that hands it back when
// nothing does.
//
// A session's `streaming` status locks its composer: Stop shows, Send is disabled, and an
// interjection queued for a server turn is held back as though this browser's own stream
// will drain it. Several producers set it: a local turn (ChatSurface's runTurn), a reattach
// (reattach.ts), and boot (chat-store derives it from a transcript with a live turn). Each
// is meant to settle it when it ends. A producer that ended without settling it, because it
// was cancelled or never mounted, left the session locked for good (#3474 fixed one such
// path).
//
// So one reconciler returns a session to idle, but only when ALL of these hold:
//   1. it reads "streaming";
//   2. no bubble in its transcript is still streaming (a live turn's preview or bubble,
//      including a lead preview with a delegate's settled rows after it);
//   3. no reattach is in flight for it;
//   4. no local turn (runTurn) is in flight for it. A local turn can have no streaming
//      bubble while it is still running: its post-stream GetTask reconcile runs after onDone
//      settled the bubble, and a pure fan-out folds its placeholder away.
// It is triggered by the events that can END a turn: a `chat.resumed` settle, a reattach
// ending, a local stream ending, a slot mounting with nothing to reattach, and the tab
// becoming visible again. It uses no timer, so it never guesses that a slow turn is over.
// The only thing it writes is the status: it never touches the steering queue, so going
// idle through it neither spends an interjection's re-check grace nor hands one back.

import { chatStore } from "./chat-store";

const localTurns = new Map<string, number>();
const reattaches = new Map<string, number>();

function claim(registry: Map<string, number>, sessionId: string): () => void {
  registry.set(sessionId, (registry.get(sessionId) ?? 0) + 1);
  let ended = false;
  return () => {
    if (ended) return; // idempotent: a cancel and the run's own unwind can both end it
    ended = true;
    const left = (registry.get(sessionId) ?? 1) - 1;
    if (left > 0) registry.set(sessionId, left);
    else registry.delete(sessionId);
  };
}

/** Register a local turn (runTurn) as in flight for `sessionId`. Returns its end. */
export function beginLocalTurn(sessionId: string): () => void {
  return claim(localTurns, sessionId);
}

/** Register a reattach as in flight for `sessionId`. Returns its end. */
export function beginReattach(sessionId: string): () => void {
  return claim(reattaches, sessionId);
}

/** Return a session reading "streaming" to idle when nothing can still settle it. Returns
 *  whether it did. Safe to call from any trigger, as often as you like: it only ever idles
 *  a session that every live producer has let go of. */
export function reconcileSessionStatus(sessionId: string): boolean {
  const snap = chatStore.getSnapshot();
  if (snap.sessionStatusMap[sessionId] !== "streaming") return false;
  if (reattaches.has(sessionId) || localTurns.has(sessionId)) return false;
  const session = snap.sessions.find((s) => s.id === sessionId);
  if (!session || session.messages.some((message) => message.status === "streaming")) return false;
  chatStore.setSessionStatus(sessionId, "idle");
  return true;
}

/** Reconcile every session reading "streaming", including ones whose slot is not mounted. */
export function reconcileAllSessionStatuses(): void {
  const statuses = chatStore.getSnapshot().sessionStatusMap;
  for (const sessionId of Object.keys(statuses)) {
    if (statuses[sessionId] === "streaming") reconcileSessionStatus(sessionId);
  }
}

/** Reconcile every session when the tab becomes visible again. A backgrounded tab can miss
 *  the event that ended a turn, and this is its chance to catch up. Returns the unsubscribe. */
export function watchSessionLiveness(): () => void {
  if (typeof document === "undefined") return () => {};
  const onVisible = () => {
    if (document.visibilityState === "hidden") return;
    reconcileAllSessionStatuses();
  };
  document.addEventListener("visibilitychange", onVisible);
  return () => document.removeEventListener("visibilitychange", onVisible);
}
