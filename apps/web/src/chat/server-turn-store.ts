import { useMemo, useSyncExternalStore } from "react";

import type { ChatMessage } from "../lib/types";

// Server-initiated turn indicator (#1767). Background push-resume (ADR 0070), scheduled
// fires, and watch reactions (ADR 0067) run a turn by self-POSTing into a session — the
// connection is held open for the WHOLE turn, but the browser only renders turns IT
// streamed, so during the agent's longest turns the console shows nothing and looks hung.
//
// The backend now brackets each such self-POST with `turn.started` / `turn.finished` bus
// events carrying the target `session_id` and an `origin` (`background-resume` /
// `scheduler` / `watch-<id>`). This tiny store tracks which sessions currently have a
// server turn in flight so ChatSessionSlot can render the EXISTING typing indicator, labelled
// by trigger, without hijacking `sessionStatusMap` (which drives the composer/send loop)
// or inserting a placeholder message. Additive and self-contained — clears on finish.

// A goal DRIVE turn run headlessly (the operator-API kick, a detach-resume, a re-arm). On the
// wire it is an ordinary `scheduler` fire — every server origin set keys off that — but its
// job id carries the `goal-run:` prefix (graph.goals.types.GOAL_RUN_JOB_PREFIX), and the job
// id travels as the turn's `trigger`. The console folds that into its own origin token so a
// goal run streams and settles like a normal turn instead of collapsing into a "Scheduled
// task" result card — the operator set the goal to watch it work.
export const GOAL_RUN_ORIGIN = "goal";
const GOAL_RUN_TRIGGER_PREFIX = "goal-run:";

// The agent's own `wait` elapsing (tools/scheduler_tools.py). Also an ordinary `scheduler`
// fire on the wire, with a `wait:<session>` job id as its trigger. The agent yielded in THIS
// chat and picks the same work back up here, so it is the conversation continuing — not a
// schedule the operator set elsewhere — and streams and settles like a normal turn.
export const WAIT_RESUME_ORIGIN = "wait-resume";
const WAIT_TRIGGER_PREFIX = "wait:";

/** The origin the console renders a server turn under: `goal` for a goal drive turn (by its
 *  `goal-run:` trigger), `wait-resume` for a `wait` wake-up (by its `wait:` trigger), else the
 *  server's own origin token unchanged. */
export function effectiveOrigin(origin: unknown, trigger: unknown): string {
  const t = String(trigger ?? "");
  if (t.startsWith(GOAL_RUN_TRIGGER_PREFIX)) return GOAL_RUN_ORIGIN;
  if (t.startsWith(WAIT_TRIGGER_PREFIX)) return WAIT_RESUME_ORIGIN;
  return String(origin ?? "");
}

/** Human label for the typing indicator, derived from the turn's origin. A watch reaction
 *  arrives as `watch-<id>` (its scheduler job id); everything else is one of the two fixed
 *  origins. Unknown origins fall back to a generic phrasing so a new backend trigger still
 *  reads sensibly instead of showing a raw token. */
export function labelForOrigin(origin: string): string {
  if (origin === GOAL_RUN_ORIGIN) return "driving the goal…";
  if (origin === WAIT_RESUME_ORIGIN) return "picking back up after a wait…";
  if (origin === "background-resume") return "responding to background reports…";
  if (origin === "scheduler") return "running a scheduled task…";
  if (origin === "a2a") return "working on a task another agent delegated…";
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

// A monotonic per-session counter bumped every time a `turn.started` arms this session (ADR
// 0130 D5). It is the FRESHNESS stamp the server-authoritative idle render compares against:
// that render reads the session index at the start of a hydration pass but applies it only
// after the per-session turn fetches await, so an idle snapshot can be stale by the time it
// lands. A `turn.started` that arms a session DURING that window advances this epoch past the
// value captured when the index was read, and the idle clear is skipped for it — a live turn
// is never wiped by an index snapshot older than it (sessionHydration.applyServerSessionState).
const startedEpochs = new Map<string, number>();

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

// The agent's own conversation continuing, not a side-channel run: its turn in response to its
// OWN background reports (ADR 0070 push-resume) or to a delegate's result. See rendersAsResultCard.
// A goal run is the same: the operator set the goal to watch the agent work toward it, so its
// turns stay full-size (tool cards and all) rather than folding into a collapsed card. So does a
// `wait` wake-up: the agent paused its own work in this chat and is resuming it.
const CONVERSATIONAL_ORIGINS = new Set(["background-resume", "delegate-result", GOAL_RUN_ORIGIN, WAIT_RESUME_ORIGIN]);

/** Whether a SETTLED server-initiated turn collapses into the compact result card (#3028).
 *  A scheduled fire, a watch reaction or an inbox/webhook trigger is a run the operator didn't
 *  start in this conversation, and #3028 keeps those visually secondary. A turn answering the
 *  agent's own background reports or a delegate's result is the conversation continuing: it
 *  streams into the chat full-size, and folding it into a differently-tinted card the moment
 *  it settled yanked the text out from under someone reading it. Those stay chat messages. */
export function rendersAsResultCard(origin: string | undefined): boolean {
  const o = (origin ?? "").trim().toLowerCase();
  return o !== "" && !CONVERSATIONAL_ORIGINS.has(o);
}

/** Short, operator-facing NOUN label for a server-initiated RESULT CARD (#3028) — distinct
 *  from `labelForOrigin`'s in-flight gerund phrasing ("running a scheduled task…"). A watch
 *  reaction arrives as `watch-<id>` (its job id); the rest are the fixed autonomous origins.
 *  Returns null for an empty origin — an operator-initiated turn, which never renders as a
 *  card — so the caller can branch on it. */
export function serverResultLabel(origin: string): string | null {
  const o = origin.trim().toLowerCase();
  if (!o) return null;
  if (o === GOAL_RUN_ORIGIN) return "Goal run";
  if (o === WAIT_RESUME_ORIGIN) return "Wait resumed";
  if (o === "scheduler") return "Scheduled task";
  if (o === "background-resume") return "Background report";
  if (o === "background") return "Background task";
  if (o.startsWith("watch-") || o === "watch") return "Watch trigger";
  if (o === "inbox") return "Inbox message";
  if (o === "webhook") return "Webhook trigger";
  if (o === "a2a") return "Delegated task";
  return "Autonomous run";
}

/** Remove common markdown formatting for compact plain-text previews, retaining line breaks. */
export function plainPreviewText(md: string): string {
  return md
    .replace(/^[ \t]*(?:>[ \t]*)+/gm, "")
    .replace(/^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+\.[ \t]+)/gm, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/`+([^`]+)`+/g, "$1")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/\b_([^_]+)_\b/g, "$1");
}

/** The collapsed card's preview line (#3028): the body of a leading `## Summary` heading when
 *  the result has one, else the head of the whole content — whitespace-flattened and clipped
 *  to `max` chars. */
export function serverResultPreview(content: string, max = 120): string {
  const source = summarySection(content) ?? content;
  const flat = plainPreviewText(source).replace(/\s+/g, " ").trim();
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
  // Advance the freshness stamp so an in-flight idle render that read the index before this
  // turn armed cannot clear it (see startedEpochs).
  startedEpochs.set(sessionId, (startedEpochs.get(sessionId) ?? 0) + 1);
  emit();
}

/** The monotonic count of `turn.started`s this console has seen for `sessionId` (0 if none).
 *  Captured when the session index is read so a later idle render can tell a turn that armed
 *  AFTER the snapshot — which it must not clear — from one the snapshot already reflects. */
export function serverTurnStartedEpoch(sessionId: string): number {
  return startedEpochs.get(sessionId) ?? 0;
}

// Server turns this console has been told ENDED, by task id, per session. The ref-count above
// answers "is ANY server turn in flight here" — which is not "is THIS turn still running" once
// two overlap. A watch reaction that closes a task queues the self-improvement review into the
// same chat before the reaction's own turn has ended: when the reaction then finishes, the count
// only drops to 1, so nothing said its preview was over. A watch turn has no `chat.resumed` to
// settle it (the reattach does), and the review's settled answer then became the last bubble —
// the reaction's preview spun for good. Naming the ended task lets the reattach pick it up.
// Bounded per session: only a preview still on screen is ever looked up.
const ENDED_TASKS_KEPT = 32;
const ended = new Map<string, string[]>();
const endedEpochs = new Map<string, number>();

/** Whether this console saw the `turn.finished` of `taskId` in `sessionId`. False for a turn an
 *  older server finished without naming its task — those keep the count-only behaviour. */
export function serverTurnEnded(sessionId: string, taskId: string): boolean {
  return !!taskId && (ended.get(sessionId)?.includes(taskId) ?? false);
}

/** A `turn.finished` for `sessionId` — disarm once the last in-flight turn settles. `taskId`
 *  (when the server named it) records WHICH turn ended, for {@link serverTurnEnded}. */
export function noteTurnFinished(sessionId: string, taskId = "") {
  if (!sessionId) return;
  if (taskId && !serverTurnEnded(sessionId, taskId)) {
    ended.set(sessionId, [...(ended.get(sessionId) ?? []), taskId].slice(-ENDED_TASKS_KEPT));
    endedEpochs.set(sessionId, (endedEpochs.get(sessionId) ?? 0) + 1);
  }
  const next = (counts.get(sessionId) ?? 0) - 1;
  if (next <= 0) {
    counts.delete(sessionId);
    labels.delete(sessionId);
  } else {
    counts.set(sessionId, next);
  }
  emit();
}

/** Drop the in-flight indicator for ONE session outright — the server reports it idle (ADR
 *  0130 D1/D5), so whatever `turn.started` armed here is over, however many turns the
 *  ref-count still believes are running. A server restart loses a turn with no `turn.finished`
 *  to decrement the count (bd-0ad5v), and nothing else would ever clear the indicator; this is
 *  the authoritative render of server state doing it. The remembered origin is left in place
 *  for a trailing `chat.resumed` to tag a settled result, exactly as `turn.finished` leaves it.
 *  Returns whether an indicator was showing. */
export function clearServerTurnsFor(sessionId: string): boolean {
  if (!sessionId || (counts.get(sessionId) ?? 0) <= 0) return false;
  counts.delete(sessionId);
  labels.delete(sessionId);
  emit();
  return true;
}

/** Test/reset hook — drop all in-flight state (including remembered origins). */
export function resetServerTurns() {
  counts.clear();
  labels.clear();
  origins.clear();
  ended.clear();
  endedEpochs.clear();
  startedEpochs.clear();
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

/** Subscribe a component to "a named server turn ended in this session". The label alone does
 *  not move when one of two overlapping turns finishes, so a slot keyed only on it never
 *  re-evaluates what it should reattach to. Returns a counter that only ever grows. */
export function useServerTurnsEnded(sessionId: string | null | undefined): number {
  return useSyncExternalStore(
    subscribe,
    () => (sessionId ? (endedEpochs.get(sessionId) ?? 0) : 0),
    () => 0,
  );
}

/** The set of sessions with a server turn in flight — for decorating ALL tabs at once (the
 *  tab bar can't call `useServerTurn` per row). Subscribes to the stable key and rebuilds
 *  the Set only when that key changes, so overlapping/other-session events don't churn it. */
export function useServerTurnSessions(): Set<string> {
  const key = useSyncExternalStore(subscribe, serverTurnSessionsKey, () => "");
  return useMemo(() => new Set(key ? key.split(",") : []), [key]);
}

/** The id given to a session's live server-turn preview (#2361). Deterministic, so a
 *  `chat.progress` frame arriving after a re-render still finds the same bubble, and so
 *  `chat.resumed` can replace it. Lives with the server-turn state it belongs to, so the
 *  chat surface can recognise a preview without importing the app-level watcher. */
export function liveMessageId(taskId: string, session: string): string {
  return `server-turn-${taskId || session}`;
}

/** True when `msg` is the live preview for this server-fired turn — the message
 *  `chat.resumed` should REPLACE rather than append a second bubble beside. */
export function isLiveServerTurn(msg: ChatMessage, taskId: string, session: string): boolean {
  return !!msg.id && msg.id === liveMessageId(taskId, session);
}
