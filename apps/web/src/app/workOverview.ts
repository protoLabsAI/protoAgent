// Work Overview derivation helpers — the pure logic behind the four overview cards
// (Goals · Watches · Tasks · Schedule): which items each card lists, the count on its
// Badge, and the one-line muted "pulse" sentence under the header. Kept out of
// WorkPanel.tsx so they're unit-testable without mounting the query layer.

import type { GoalState, ScheduledJob, Task, WatchState } from "../lib/types";

// ── goals ────────────────────────────────────────────────────────────────────

/** Goals still in flight — status "active" is the only non-terminal state (the backend sets
 *  `finished_at` alongside every terminal status: achieved/exhausted/unachievable). The
 *  `finished_at` guard is belt-and-braces against a corrupt record. */
export function activeGoals(goals: GoalState[]): GoalState[] {
  return goals.filter((g) => g.status === "active" && !g.finished_at);
}

/** "2 driving · iteration 3/6" — count of active goals + the furthest-along loop.
 *  Empty string when nothing is active (the card shows its Empty state instead). */
export function goalsPulse(goals: GoalState[]): string {
  const active = activeGoals(goals);
  if (!active.length) return "";
  const lead = active.reduce((a, b) => ((b.iteration ?? 0) > (a.iteration ?? 0) ? b : a));
  return `${active.length} driving · iteration ${lead.iteration ?? 0}/${lead.max_iterations ?? "∞"}`;
}

// ── watches ──────────────────────────────────────────────────────────────────

/** Watches worth showing on the card: actives first, then the finished ones still inside
 *  the `watches.keep_terminal_h` retention window (the server prunes past it, so the card
 *  no longer has to reason about an unbounded tail).
 *
 *  There is deliberately no "cleared" filter here: clearing a watch UNLINKS its file, so a
 *  cleared watch never comes back from the API. This used to filter `status !== "cleared"`,
 *  which never matched anything. */
export function visibleWatches(watches: WatchState[]): WatchState[] {
  return [
    ...watches.filter((w) => w.status === "active"),
    ...watches.filter((w) => w.status !== "active"),
  ];
}

export function activeWatches(watches: WatchState[]): WatchState[] {
  return watches.filter((w) => w.status === "active");
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
  if (watch.interval_s) parts.push(`every ${formatWatchDuration(watch.interval_s)}`);
  if (watch.deadline != null) {
    const left = watch.deadline - now / 1000;
    parts.push(left > 0 ? `expires in ${formatWatchDuration(left)}` : "past its deadline");
  }
  if (watch.stall_after) parts.push(`stall after ${watch.stall_after}`);
  return parts;
}

/** Whether the deadline has lapsed — the panel tints this segment, since it means the
 *  watch is about to finish `expired` rather than met. */
export function watchDeadlineLapsed(watch: WatchState, now: number = Date.now()): boolean {
  return watch.status === "active" && watch.deadline != null && watch.deadline - now / 1000 <= 0;
}

/** "2 watching · 1 met today" (the met fragment only when something was met today).
 *  The met time is `finished_at` — the controller's met path finishes BEFORE its
 *  `last_checked = now` write, so `last_checked` on a met watch is the PREVIOUS check
 *  (or unset when it met on the first one). Both are epoch seconds; "today" is the
 *  local calendar day of `now`. */
export function watchesPulse(watches: WatchState[], now: number = Date.now()): string {
  const watching = activeWatches(watches).length;
  const dayStart = new Date(now);
  dayStart.setHours(0, 0, 0, 0);
  const metToday = watches.filter((w) => {
    if (w.status !== "met") return false;
    const met = (w.finished_at ?? w.last_checked ?? 0) * 1000;
    return met >= dayStart.getTime() && met <= now;
  }).length;
  if (!watching && !metToday) return "";
  const head = `${watching} watching`;
  return metToday ? `${head} · ${metToday} met today` : head;
}

// ── tasks ────────────────────────────────────────────────────────────────────

const normStatus = (s: string | undefined) => (s ?? "").toLowerCase().replace(/[ _-]/g, "");

/** The card's working set: in-progress first, then ready-to-pick-up (`open`, plus a
 *  literal `ready` for beads-shaped feeds). Blocked/deferred/closed stay off the card. */
export function taskBuckets(issues: Task[]): { ready: Task[]; inProgress: Task[] } {
  const ready = issues.filter((i) => ["open", "ready"].includes(normStatus(i.status)));
  const inProgress = issues.filter((i) => normStatus(i.status) === "inprogress");
  return { ready, inProgress };
}

/** "3 ready · 1 in progress"; empty string when there's no open work. */
export function tasksPulse(issues: Task[]): string {
  const { ready, inProgress } = taskBuckets(issues);
  if (!ready.length && !inProgress.length) return "";
  return `${ready.length} ready · ${inProgress.length} in progress`;
}

// ── schedule ─────────────────────────────────────────────────────────────────

/** Enabled jobs with a computed next fire, soonest first. */
export function upcomingJobs(jobs: ScheduledJob[]): ScheduledJob[] {
  return jobs
    .filter((j) => j.enabled !== false && j.next_fire)
    .sort((a, b) => ((a.next_fire ?? "") < (b.next_fire ?? "") ? -1 : 1));
}

/** "next in 25m" from the soonest upcoming fire; empty string when nothing is armed. */
export function schedulePulse(jobs: ScheduledJob[], now: number = Date.now()): string {
  const next = upcomingJobs(jobs)[0];
  if (!next) return "";
  const rel = untilLabel(next.next_fire, now);
  return rel ? `next ${rel}` : "";
}

/** Future-relative time, e.g. "in 5m" / "in 3h" / "in 2d" — `ago()`'s forward twin.
 *  A past/imminent fire (the scheduler hasn't recomputed yet) reads "due now". */
export function untilLabel(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = (t - now) / 1000;
  if (s < 60) return "due now";
  if (s < 3600) return `in ${Math.round(s / 60)}m`;
  if (s < 86400) return `in ${Math.round(s / 3600)}h`;
  return `in ${Math.round(s / 86400)}d`;
}
