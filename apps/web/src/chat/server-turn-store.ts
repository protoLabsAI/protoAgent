import { useMemo, useSyncExternalStore } from "react";

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

// Raw trigger origin per session, remembered from `turn.started` (#3028). Kept SEPARATELY
// from the ref-counted indicator state above because it must OUTLIVE `turn.finished`: the
// terminal `chat.resumed` — which tags its settled message so ChatMessageView renders it as a
// compact result card — can land after the indicator has already cleared. Last-writer per
// session; a short list of open chats, so no eviction is needed.
const origins = new Map<string, string>();

/** Remember the raw trigger origin for `sessionId` (from `turn.started`) so the terminal
 *  `chat.resumed` can tag its settled message even after `turn.finished` clears the indicator. */
export function rememberOrigin(sessionId: string, origin: string) {
  if (!sessionId || !origin) return;
  origins.set(sessionId, origin);
}

/** The last remembered trigger origin for `sessionId`, or "" — the fallback ChatResumeWatch
 *  uses to tag a settled server-turn message when the server didn't stamp `origin` on the
 *  `chat.resumed` event (an older backend that predates #3028). */
export function originForSession(sessionId: string): string {
  return origins.get(sessionId) ?? "";
}

/** Short, operator-facing NOUN label for a server-initiated RESULT CARD (#3028) — distinct
 *  from `labelForOrigin`'s in-flight gerund phrasing ("running a scheduled task…"). A watch
 *  reaction arrives as `watch-<id>` (its job id); the rest are the fixed autonomous origins.
 *  Returns null for an empty origin — an operator-initiated turn, which never renders as a
 *  card — so the caller can branch on it. */
export function serverResultLabel(origin: string): string | null {
  const o = origin.trim().toLowerCase();
  if (!o) return null;
  if (o === "scheduler") return "Scheduled task";
  if (o === "background-resume") return "Background report";
  if (o === "background") return "Background task";
  if (o.startsWith("watch-") || o === "watch") return "Watch trigger";
  if (o === "inbox") return "Inbox message";
  if (o === "webhook") return "Webhook trigger";
  return "Autonomous run";
}

/** The collapsed card's preview line (#3028): the body of a leading `## Summary` heading when
 *  the result has one, else the head of the whole content — whitespace-flattened and clipped
 *  to `max` chars. */
export function serverResultPreview(content: string, max = 120): string {
  const source = summarySection(content) ?? content;
  const flat = source.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max).trimEnd()}…` : flat;
}

/** Body of a leading `## Summary` (any heading level, optional trailing colon) — the lines up
 *  to the next heading — or null when the content has no such section. */
function summarySection(md: string): string | null {
  const lines = md.split("\n");
  const start = lines.findIndex((l) => /^#{1,6}\s+summary\s*:?\s*$/i.test(l.trim()));
  if (start < 0) return null;
  const body: string[] = [];
  for (let i = start + 1; i < lines.length; i++) {
    if (/^#{1,6}\s+/.test(lines[i].trim())) break;
    body.push(lines[i]);
  }
  const text = body.join("\n").trim();
  return text || null;
}

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

/** Test/reset hook — drop all in-flight state (including remembered origins). */
export function resetServerTurns() {
  counts.clear();
  labels.clear();
  origins.clear();
  emit();
}

/** The active indicator label for `sessionId`, or null when no server turn is in flight. */
export function serverTurnLabel(sessionId: string): string | null {
  return (counts.get(sessionId) ?? 0) > 0 ? (labels.get(sessionId) ?? null) : null;
}

/** A stable snapshot of every session with a server turn in flight: the ids sorted +
 *  comma-joined, so getSnapshot stays Object.is-equal while the SET is unchanged (a
 *  `turn.started`/`finished` on an already-counted session yields the same key and skips
 *  the re-render). `counts` only holds sessions with count > 0, so this is inherently
 *  overlap-safe — two nudges on one session collapse to one entry that clears only when
 *  the last settles. */
export function serverTurnSessionsKey(): string {
  return [...counts.keys()].sort().join(",");
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

/** The set of sessions with a server turn in flight — for decorating ALL tabs at once (the
 *  tab bar can't call `useServerTurn` per row). Subscribes to the stable key and rebuilds
 *  the Set only when that key changes, so overlapping/other-session events don't churn it. */
export function useServerTurnSessions(): Set<string> {
  const key = useSyncExternalStore(subscribe, serverTurnSessionsKey, () => "");
  return useMemo(() => new Set(key ? key.split(",") : []), [key]);
}
