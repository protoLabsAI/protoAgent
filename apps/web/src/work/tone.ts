import type { Status } from "@protolabsai/ui/primitives";
import type { ScheduledJob } from "../lib/types";

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
