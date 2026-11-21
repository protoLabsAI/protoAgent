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

/** A row/detail heading never exceeds this; the full text belongs in a body field. */
const SUMMARY_MAX = 80;

/** A short heading from free text — a page's message, a prompt — for rows and detail
 *  titles that must never show the whole thing: the first sentence of the first non-empty
 *  line, whitespace collapsed, clipped to ~{@link SUMMARY_MAX} characters with an ellipsis.
 *  Empty in → empty out, so each caller keeps its own kind fallback ("Delivery"). */
export function summaryTitle(text: string | undefined): string {
  const line = (text ?? "").split("\n").map((part) => part.trim()).find(Boolean) ?? "";
  const collapsed = line.replace(/\s+/g, " ");
  const sentence = /^.*?[.!?](?=\s|$)/.exec(collapsed)?.[0] ?? collapsed;
  return sentence.length > SUMMARY_MAX ? `${sentence.slice(0, SUMMARY_MAX - 1).trimEnd()}…` : sentence;
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

/** A job's own clock (work-view-additions §2.3): its `timezone`, or UTC when that is null.
 *  An unusable zone name falls back to UTC rather than throwing out of a render. */
export function jobZone(job: Pick<ScheduledJob, "timezone">): string {
  const zone = (job.timezone || "").trim();
  if (!zone) return "UTC";
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    return zone;
  } catch {
    return "UTC";
  }
}

/** The scheduler holds a zone-less ISO stamp as UTC, so it is read as UTC here too —
 *  never as the viewer's local time. */
function parseStamp(iso: string): Date {
  const text = (iso || "").trim();
  const naive = /^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}/.test(text) && !/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(text);
  return new Date(naive ? `${text.replace(" ", "T")}Z` : text);
}

function zoneParts(date: Date, zone: string): Record<string, string> {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone, year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date);
  return Object.fromEntries(parts.map((part) => [part.type, part.value]));
}

/** One time in a job's clock, zone name included: "Today 14:00 UTC", "Nov 16 09:00
 *  Europe/Berlin" (the year joins the date once it isn't this year). `capital: false`
 *  lowers "today" for mid-sentence use. "" for an unreadable stamp. */
export function jobClockTime(iso: string | null | undefined, zone: string, now: number = Date.now(), capital = true): string {
  const date = parseStamp(iso ?? "");
  if (!Number.isFinite(date.getTime())) return "";
  const at = zoneParts(date, zone);
  const current = zoneParts(new Date(now), zone);
  const sameYear = at.year === current.year;
  const day = sameYear && at.month === current.month && at.day === current.day
    ? (capital ? "Today" : "today")
    : `${at.month} ${at.day}${sameYear ? "" : ` ${at.year}`}`;
  return `${day} ${at.hour}:${at.minute} ${zone}`;
}

/** The health note a failing job's row meta ends with (§2.2): "failed the last N runs", or
 *  "the last run failed" for one. "" while the job is healthy. */
export function scheduleFailureNote(job: Pick<ScheduledJob, "consecutive_failures">): string {
  const failures = Math.floor(Number(job.consecutive_failures) || 0);
  if (failures < 1) return "";
  return failures === 1 ? "the last run failed" : `failed the last ${failures} runs`;
}

/** {@link scheduleWhen} in the job's own clock (§2.3 "one clock"): a recurring job's cron
 *  already reads in its zone; a one-shot is an instant, so it is placed in the job's clock
 *  and carries the zone name. `withZone` names the zone on a recurring job's time too. */
export function scheduleWhenInJobClock(job: Pick<ScheduledJob, "schedule" | "timezone">, withZone = false, now: number = Date.now()): string {
  const when = scheduleWhen(job.schedule);
  if (parseSchedule(job.schedule).mode === "once") {
    const at = jobClockTime(job.schedule, jobZone(job), now);
    return at ? `Once, ${at}` : when;
  }
  return withZone && when !== "Custom schedule" ? `${when} (${jobZone(job)})` : when;
}

/** A schedule row's meta: its timing in the job's clock, ending with the health note when
 *  the job is failing ("Every day at 09:00 · failed the last 3 runs", §2.2). */
export function scheduleMeta(job: Pick<ScheduledJob, "schedule" | "timezone" | "consecutive_failures">, now: number = Date.now()): string {
  const note = scheduleFailureNote(job);
  const when = scheduleWhenInJobClock(job, false, now);
  return note ? `${when} · ${note}` : when;
}
