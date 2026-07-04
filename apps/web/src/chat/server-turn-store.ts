import { useSyncExternalStore } from "react";

// Server-initiated turn indicator (#1767). Background push-resume (ADR 0070), scheduled
// fires, and watch reactions (ADR 0067) run a turn by self-POSTing into a session — the
// connection is held open for the WHOLE turn, but the browser only renders turns IT
// streamed, so during the agent's longest turns the console shows nothing and looks hung.
//
// The backend now brackets each such self-POST with `turn.started` / `turn.finished` bus
// events carrying the target `session_id` and an `origin` (`background-resume` /
// `scheduler` / `watch-<id>`). This tiny store tracks which sessions currently have a
// server turn in flight so ChatSurface can render the EXISTING typing indicator, labelled
// by trigger, without hijacking `sessionStatusMap` (which drives the composer/send loop)
// or inserting a placeholder message. Additive and self-contained — clears on finish.

/** Human label for the typing indicator, derived from the turn's origin. A watch reaction
 *  arrives as `watch-<id>` (its scheduler job id); everything else is one of the two fixed
 *  origins. Unknown origins fall back to a generic phrasing so a new backend trigger still
 *  reads sensibly instead of showing a raw token. */
export function labelForOrigin(origin: string): string {
  if (origin === "background-resume") return "responding to background reports…";
  if (origin === "scheduler") return "running a scheduled task…";
  if (origin.startsWith("watch-") || origin === "watch") return "reacting to a triggered watch…";
  return "responding to a background trigger…";
}

// Ref-counted per session: two nudges can target one session (the A2A server serializes
// the turns, but both `turn.started`s can land before either finishes), so a plain
// last-writer map would clear the indicator while a second turn is still running. Count up
// on start, down on finish; show while count > 0, using the most-recent label.
const counts = new Map<string, number>();
const labels = new Map<string, string>();
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((fn) => fn());
}

/** A `turn.started` for `sessionId` — arm the indicator with `label`. */
export function noteTurnStarted(sessionId: string, label: string) {
  if (!sessionId) return;
  counts.set(sessionId, (counts.get(sessionId) ?? 0) + 1);
  labels.set(sessionId, label);
  emit();
}

/** A `turn.finished` for `sessionId` — disarm once the last in-flight turn settles. */
export function noteTurnFinished(sessionId: string) {
  if (!sessionId) return;
  const next = (counts.get(sessionId) ?? 0) - 1;
  if (next <= 0) {
    counts.delete(sessionId);
    labels.delete(sessionId);
  } else {
    counts.set(sessionId, next);
  }
  emit();
}

/** Test/reset hook — drop all in-flight state. */
export function resetServerTurns() {
  counts.clear();
  labels.clear();
  emit();
}

/** The active indicator label for `sessionId`, or null when no server turn is in flight. */
export function serverTurnLabel(sessionId: string): string | null {
  return (counts.get(sessionId) ?? 0) > 0 ? (labels.get(sessionId) ?? null) : null;
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Subscribe a component to the server-turn label for one session. getSnapshot returns the
 *  label string (or ""), so a bus event that changed a DIFFERENT session yields the same
 *  value here (stable by Object.is) and skips this component's re-render. */
export function useServerTurn(sessionId: string | null | undefined): string | null {
  const label = useSyncExternalStore(
    subscribe,
    () => (sessionId ? (serverTurnLabel(sessionId) ?? "") : ""),
    () => "",
  );
  return label || null;
}
