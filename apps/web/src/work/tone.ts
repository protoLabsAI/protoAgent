import type { Status } from "@protolabsai/ui/primitives";
import type { ScheduledJob, WatchState } from "../lib/types";
import type { DecisionRow } from "../lib/api/decisions";
import type { WorkItem } from "./useWorkItems";

export type WorkGroupId = "needs-you" | "doing-now" | "coming-up" | "instructions";
export type WorkJobRef = { kind: "task" | "background" | "goal"; id: string };

/** Adapter facts, not inferred associations. Today's Task/DirectiveRow APIs do not
 * carry start history or job links; future adapters can supply them explicitly. */
export type GroupableWorkItem = (WorkItem & {
  started?: boolean;
  tiedJob?: WorkJobRef;
}) | (Omit<WorkItem, "kind" | "raw"> & (
  | { kind: "delivery"; raw: { delivered: boolean; needsHuman: boolean } }
  | { kind: "wait"; raw: { state: "pending" | "completed" | "cancelled" } }
));

/** Pure classification on source state: WorkStatus deliberately loses task detail. */
export function groupFor(item: GroupableWorkItem): WorkGroupId | null {
  const raw = item.raw as Record<string, unknown> | null;
  switch (item.kind) {
    case "decision": return raw?.state === "pending" ? "needs-you" : null;
    case "delivery": return !item.raw.delivered && item.raw.needsHuman ? "needs-you" : null;
    case "goal":
      if (raw?.status === "active") return "doing-now";
      return raw?.status === "exhausted" || raw?.status === "unachievable" ? "needs-you" : null;
    case "background":
      if (raw?.status === "running") return "doing-now";
      return raw?.status === "open" || raw?.status === "deferred" ? "coming-up" : null;
    case "task":
      if (raw?.status === "in_progress") return "doing-now";
      if (raw?.status === "blocked") return item.started === true ? "doing-now" : "coming-up";
      return !raw?.status || raw.status === "open" || raw.status === "deferred" ? "coming-up" : null;
    case "watch":
      return raw?.status === "active" || (raw?.status === "met" && raw.repeat === true) ? "coming-up" : null;
    case "schedule":
      return ["scheduled", "paused", "waiting"].includes(item.status) ? "coming-up" : null;
    case "wait": return item.raw.state === "pending" ? "coming-up" : null;
    case "directive": return raw?.status === "in_force" ? "instructions" : null;
    case "activity": return null;
  }
}

export type WorkStatus = "needs-you" | "running" | "waiting" | "scheduled" | "paused" | "done" | "failed" | "cancelled";
export type WorkLane = "needs-you" | "doing" | "waiting" | "next" | "done";

export function statusTone(status: WorkStatus): Status {
  switch (status) {
    case "needs-you": return "warning";
    case "running": return "info";
    case "done": return "success";
    case "failed": return "error";
    default: return "neutral";
  }
}

export function statusWord(status: WorkStatus): string {
  return status === "needs-you" ? "needs you" : status;
}

export function laneFor(status: WorkStatus): WorkLane {
  switch (status) {
    case "needs-you": return "needs-you";
    case "running": return "doing";
    case "waiting": return "waiting";
    case "scheduled": case "paused": return "next";
    default: return "done";
  }
}

const warned = new Set<string>();
function unknown(kind: string, raw: string): WorkStatus {
  const key = `${kind}:${raw}`;
  if (import.meta.env.DEV && !warned.has(key)) {
    warned.add(key);
    console.warn(`[work] Unknown ${kind} status: ${raw}`);
  }
  return "waiting";
}
function map(kind: string, raw: string, values: Record<string, WorkStatus>): WorkStatus {
  return Object.prototype.hasOwnProperty.call(values, raw) ? values[raw] : unknown(kind, raw);
}

// GoalState.status is a string; the controller persists active through verifier waits.
// Callers with a live turn signal can distinguish those waits without inventing a raw status.
export function fromGoal(raw: string, turnRunning = true): WorkStatus {
  if (raw === "active") return turnRunning ? "running" : "waiting";
  return map("goal", raw, { achieved: "done", exhausted: "failed", unachievable: "failed", cleared: "cancelled" });
}
export function fromWatch(raw: string, repeat = false): WorkStatus {
  if (raw === "met" && repeat) return "waiting";
  return map("watch", raw, { active: "waiting", met: "done", expired: "cancelled", cleared: "cancelled" });
}
export function fromTask(raw: string): WorkStatus {
  return map("task", raw, { in_progress: "running", blocked: "waiting", open: "scheduled", deferred: "paused", closed: "done" });
}
export function fromSchedule(job: Pick<ScheduledJob, "schedule" | "enabled" | "next_fire" | "last_fire">): WorkStatus {
  // A completed one-shot may be disabled by its scheduler backend.
  const oneShot = /^\d{4}-\d{2}-\d{2}(?:T|\s)/.test(job.schedule.trim());
  if (oneShot && job.last_fire && !job.next_fire) return "done";
  if (job.enabled === false) return "paused";
  if (job.next_fire) return "scheduled";
  return unknown("schedule", "missing next fire");
}
export function fromBackgroundJob(raw: string): WorkStatus {
  return map("background job", raw, { running: "running", completed: "done", failed: "failed", canceled: "cancelled" });
}
// Activity stores the terminal TurnOutcome.state, not the spec's prose finished/errored.
export function fromActivity(raw: string): WorkStatus {
  return map("activity", raw, { completed: "done", failed: "failed", canceled: "cancelled" });
}
export function fromDecision(raw: string): WorkStatus {
  return map("decision", raw, { pending: "needs-you", answered: "done", expired: "cancelled", cancelled: "cancelled", withdrawn: "cancelled" });
}
// A standing directive (graph/directives): in force until the agent fulfils it or the
// operator revokes it — a passive constraint, so an active one waits like a watch.
export function fromDirective(raw: string): WorkStatus {
  return map("directive", raw, { in_force: "waiting", fulfilled: "done", revoked: "cancelled" });
}

/** `now` is epoch milliseconds (Date.now()); numeric input timestamps are epoch seconds.
 * The tooltip is a full local timestamp, ready for the DS Tooltip's content prop. */
export function relTime(epochOrIso: number | string, now: number = Date.now()): { text: string; tooltip: string } {
  const date = new Date(typeof epochOrIso === "number" ? epochOrIso * 1000 : epochOrIso);
  if (!Number.isFinite(date.getTime())) return { text: "", tooltip: "" };
  const current = new Date(now);
  const minutes = Math.floor((now - date.getTime()) / 60_000);
  const time = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const sameDay = date.toDateString() === current.toDateString();
  const day = date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  const text = minutes >= 0 && minutes < 60
    ? `${minutes} min ago`
    : `${sameDay ? "today" : day} at ${time}`;
  return { text, tooltip: date.toLocaleString("en-GB", { dateStyle: "full", timeStyle: "long" }) };
}

/** Who answered, in plain words: the operator is "you"; otherwise the agent/name as given. */
function answeredBy(row: DecisionRow): string {
  if (!row.answered_by) return "";
  return row.answered_by === "operator" || row.answered_by === "console" ? "you" : row.answered_by;
}

/**
 * The single meta line under a decision (§2.4). It feeds BOTH the Decisions section row and the
 * Overview "Needs you" pin, which share these fields: "Asked {time}" while pending, "Answered:
 * {label} · by {who} · {time}" once answered, and "Withdrawn: {reason}" when closed without an
 * answer. The agent (field 5) is prepended only in the agent-wide ("All chats") filter; the
 * Overview pin is filter-agnostic (`allChats` false), so it shows "Asked {time}" and no agent.
 * Never carries an id of any kind (§2.3, §2.4 "Never shown").
 */
export function decisionMeta(row: DecisionRow, allChats: boolean): string {
  const parts: string[] = [];
  if (allChats && row.agent) parts.push(row.agent);
  if (row.state === "pending") {
    const asked = relTime(row.created_at).text;
    if (asked) parts.push(`Asked ${asked}`);
  } else if (row.state === "answered") {
    const label = row.answer_label ?? row.answer ?? "an option";
    const who = answeredBy(row);
    const when = relTime(row.answered_at ?? row.created_at).text;
    parts.push(`Answered: ${label}${who ? ` · by ${who}` : ""}${when ? ` · ${when}` : ""}`);
  } else {
    // expired / cancelled / withdrawn — all map to the "cancelled" badge (§2.4 field 2).
    const reason = row.withdraw_reason?.trim();
    if (reason) parts.push(`Withdrawn: ${reason}`);
  }
  return parts.join(" · ");
}

/** A compact span: `45s` / `30m` / `2h` / `3d`. Mirrors `_duration` in
 *  `graph/watches/types.py` — the same watch reads the same way in the console and in the
 *  agent's own `list_watches`, so an operator and the agent describe it identically.
 *  Rounded on purpose: this is prose, not arithmetic. */
export function formatWatchDuration(seconds: number): string {
  const s = Math.max(0, seconds);
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/** The lifetime facts an ACTIVE watch carries — `["every 30m", "expires in 2h",
 *  "stall after 3"]` — for the panel's meta line. Mirrors `Watch._lifetime_suffix`.
 *
 *  Terminal watches return `[]`: "expires in …" is meaningless once a watch is met or
 *  expired, which is the same rule the server applies before building its status line.
 *  A deadline already in the past reads `past its deadline` rather than `expires in 0s` —
 *  the tick just hasn't run yet. */
export function watchLifetime(watch: WatchState, now: number = Date.now()): string[] {
  if (watch.status !== "active") return [];
  const parts: string[] = [];
  // Lead with the disposition: a repeating/change watch behaves differently enough from a
  // one-shot tripwire that it shouldn't look identical in the list. Mirrors the server's
  // `_lifetime_suffix`.
  if (watch.trigger === "change") parts.push("on change");
  if (watch.repeat) parts.push("repeating");
  if (watch.interval_s) parts.push(`every ${formatWatchDuration(watch.interval_s)}`);
  if (watch.deadline != null) {
    const left = watch.deadline - now / 1000;
    parts.push(left > 0 ? `expires in ${formatWatchDuration(left)}` : "past its deadline");
  }
  if (watch.stall_after) parts.push(`stall after ${watch.stall_after}`);
  // A repeating watch that has fired more than a handful of times is worth seeing at a
  // glance — each fire can enqueue an agent turn, so a high count is a cost signal, not
  // trivia. Only shown once it's non-trivial, to keep a normal row quiet.
  if (watch.repeat && (watch.fire_count ?? 0) > 1) parts.push(`fired ${watch.fire_count}×`);
  return parts;
}
