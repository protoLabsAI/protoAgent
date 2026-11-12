/**
 * Plain-words row meta for Work items, shared by the Overview lanes (useWorkItems) and the
 * per-kind sections, so a row reads the same wherever it shows. Raw values (a cron string, a
 * bare "task", a watch's trigger keyword) never reach a row (work-view spec §2, §4).
 */
import { issueType } from "../app/tasks";
import type { GoalState, ScheduledJob, Task, WatchState } from "../lib/types";
import { WEEKDAYS, parseSchedule } from "../schedule/schedule-builder";

/** A goal is a job with a finish line (ADR D3, work-consolidation §2). Progress reads "step N
 *  of M" — with no limit, just "step N", NEVER "n/0" or "∞". An active goal re-checks its
 *  finish line after each turn, so its progress line says so; a stopped one shows only how far
 *  it got. The group rows and the goal detail share this one line, so both read the same
 *  "step N" — never the verifier type, which stays in Technical details (§2.5). */
export function goalProgress(g: GoalState): string {
  const iter = g.iteration ?? 0;
  const max = g.max_iterations;
  const steps = max && max > 0 ? `step ${iter} of ${max}` : `step ${iter}`;
  return g.status === "active" ? `${steps} · checking after each turn` : steps;
}

/** "Fires once when met" / "Fires every time it's met" / "Fires when the value changes". */
export function watchKind(w: WatchState): string {
  if (w.trigger === "change") return "Fires when the value changes";
  if (w.repeat) return "Fires every time it's met";
  return "Fires once when met";
}

/** Numeric priority → word (§2.7 field 3). Only 0–3 are defined; anything else is blank. */
export function priorityWord(priority: Task["priority"]): string {
  const n = typeof priority === "number" ? priority : parseInt(String(priority ?? ""), 10);
  return ({ 0: "urgent", 1: "high", 2: "normal", 3: "low" } as Record<number, string>)[n] ?? "";
}

function capitalize(value: string): string {
  return value ? value[0].toUpperCase() + value.slice(1) : value;
}

/** "Bug · high", or just the type when the priority has no word (§2.7 field 3). */
export function typeAndPriority(task: Task): string {
  const type = capitalize(issueType(task));
  const word = priorityWord(task.priority);
  return word ? `${type} · ${word}` : type;
}

/** Plain-words timing for a schedule string — reuses the builder's `parseSchedule` so the
 *  raw cron is never surfaced (§2.8 field 3). A custom cron that matches no friendly preset
 *  reads "Custom schedule"; the cron itself stays in Technical details. */
export function scheduleWhen(schedule: ScheduledJob["schedule"]): string {
  const parsed = parseSchedule(schedule);
  if (parsed.mode === "once") {
    const d = new Date((schedule || "").trim());
    if (Number.isNaN(d.getTime())) return "Once";
    const day = d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    return `Once, ${day} at ${time}`;
  }
  if (parsed.mode === "repeat") {
    const minute = parsed.time.split(":")[1] ?? "00";
    switch (parsed.freq) {
      case "hourly":
        return `Every hour at :${minute}`;
      case "daily":
        return `Every day at ${parsed.time}`;
      case "weekdays":
        return `Weekdays at ${parsed.time}`;
      case "weekly":
        return `${WEEKDAYS[parsed.dow]}s at ${parsed.time}`;
    }
  }
  return "Custom schedule";
}
