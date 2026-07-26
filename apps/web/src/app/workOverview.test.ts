import { describe, expect, it } from "vitest";

import type { GoalState, ScheduledJob, Task, WatchState } from "../lib/types";
import {
  activeGoals,
  activeWatches,
  goalsPulse,
  schedulePulse,
  taskBuckets,
  tasksPulse,
  untilLabel,
  upcomingJobs,
  visibleWatches,
  watchLifetime,
  watchDeadlineLapsed,
  formatWatchDuration,
  watchesPulse,
} from "./workOverview";

const goal = (over: Partial<GoalState>): GoalState => ({
  session_id: "s",
  condition: "c",
  status: "active",
  ...over,
});

const watch = (over: Partial<WatchState>): WatchState => ({
  id: "w",
  condition: "c",
  status: "active",
  ...over,
});

const task = (over: Partial<Task>): Task => ({ id: "t", title: "t", ...over });

const job = (over: Partial<ScheduledJob>): ScheduledJob => ({
  id: "j",
  prompt: "p",
  schedule: "0 9 * * *",
  ...over,
});

describe("goals card", () => {
  it("activeGoals keeps only status:active, unfinished goals", () => {
    // The backend's terminal statuses are achieved / exhausted / unachievable (each set
    // alongside finished_at) — "active" is the only in-flight state.
    const goals = [
      goal({ session_id: "a" }),
      goal({ session_id: "b", status: "achieved" }),
      goal({ session_id: "c", status: "exhausted" }),
      goal({ session_id: "e", status: "unachievable" }),
      goal({ session_id: "d", finished_at: 123 }),
    ];
    expect(activeGoals(goals).map((g) => g.session_id)).toEqual(["a"]);
  });

  it("pulse reports the count and the furthest-along iteration", () => {
    const goals = [
      goal({ session_id: "a", iteration: 1, max_iterations: 6 }),
      goal({ session_id: "b", iteration: 3, max_iterations: 8 }),
      goal({ session_id: "c", status: "achieved", iteration: 9, max_iterations: 9 }),
    ];
    expect(goalsPulse(goals)).toBe("2 driving · iteration 3/8");
  });

  it("pulse tolerates missing iteration fields and empty lists", () => {
    expect(goalsPulse([goal({})])).toBe("1 driving · iteration 0/∞");
    expect(goalsPulse([])).toBe("");
    expect(goalsPulse([goal({ status: "achieved" })])).toBe("");
  });
});

describe("watches card", () => {
  // Fixed "now": 2026-07-01T12:00:00 local time.
  const now = new Date(2026, 6, 1, 12, 0, 0).getTime();
  const secsAt = (h: number) => new Date(2026, 6, 1, h, 0, 0).getTime() / 1000;

  it("visibleWatches floats actives first, keeps the finished ones in order", () => {
    // Only `active` / `met` / `expired` ever reach the client — clearing a watch unlinks
    // its file, so there is no "cleared" row to filter out (the filter that used to try
    // was dead code).
    const ws = [
      watch({ id: "m", status: "met" }),
      watch({ id: "a", status: "active" }),
      watch({ id: "e", status: "expired" }),
      watch({ id: "a2", status: "active" }),
    ];
    expect(visibleWatches(ws).map((w) => w.id)).toEqual(["a", "a2", "m", "e"]);
    expect(activeWatches(ws)).toHaveLength(2);
  });

  it("pulse counts watching + met-today (local day) off finished_at", () => {
    const ws = [
      watch({ id: "a" }),
      watch({ id: "b" }),
      watch({ id: "m1", status: "met", finished_at: secsAt(9) }), // this morning
      watch({ id: "m2", status: "met", finished_at: secsAt(9) - 86_400 }), // yesterday
    ];
    expect(watchesPulse(ws, now)).toBe("2 watching · 1 met today");
  });

  it("met time is finished_at, not the stale pre-met last_checked (falls back when absent)", () => {
    // The controller's met path finishes BEFORE the `last_checked = now` write: a watch
    // that met on its FIRST check has no last_checked at all, and one that met later
    // still carries the previous check's time (possibly yesterday).
    const firstCheckMet = watch({ id: "f", status: "met", finished_at: secsAt(9) });
    const metAfterYesterdaysCheck = watch({
      id: "y",
      status: "met",
      finished_at: secsAt(9),
      last_checked: secsAt(9) - 86_400,
    });
    expect(watchesPulse([watch({}), firstCheckMet], now)).toBe("1 watching · 1 met today");
    expect(watchesPulse([watch({}), metAfterYesterdaysCheck], now)).toBe("1 watching · 1 met today");
    // Older payloads without finished_at still count via last_checked.
    const legacy = watch({ id: "l", status: "met", last_checked: secsAt(9) });
    expect(watchesPulse([watch({}), legacy], now)).toBe("1 watching · 1 met today");
  });

  it("pulse omits the met fragment when nothing was met today", () => {
    expect(watchesPulse([watch({})], now)).toBe("1 watching");
    expect(watchesPulse([], now)).toBe("");
    expect(watchesPulse([watch({ status: "expired" })], now)).toBe("");
  });
});

describe("tasks card", () => {
  it("buckets open/ready vs in-progress and ignores the rest", () => {
    const issues = [
      task({ id: "1", status: "open" }),
      task({ id: "2", status: "ready" }),
      task({ id: "3", status: "in_progress" }),
      task({ id: "4", status: "blocked" }),
      task({ id: "5", status: "deferred" }),
      task({ id: "6", status: "closed" }),
    ];
    const { ready, inProgress } = taskBuckets(issues);
    expect(ready.map((i) => i.id)).toEqual(["1", "2"]);
    expect(inProgress.map((i) => i.id)).toEqual(["3"]);
    expect(tasksPulse(issues)).toBe("2 ready · 1 in progress");
  });

  it("pulse is empty with no open work", () => {
    expect(tasksPulse([task({ status: "closed" })])).toBe("");
    expect(tasksPulse([])).toBe("");
  });
});

describe("schedule card", () => {
  const now = Date.parse("2026-07-01T12:00:00Z");

  it("upcomingJobs keeps enabled+armed jobs, soonest first", () => {
    const jobs = [
      job({ id: "late", next_fire: "2026-07-02T09:00:00Z" }),
      job({ id: "soon", next_fire: "2026-07-01T12:30:00Z" }),
      job({ id: "off", enabled: false, next_fire: "2026-07-01T12:10:00Z" }),
      job({ id: "unarmed", next_fire: null }),
    ];
    expect(upcomingJobs(jobs).map((j) => j.id)).toEqual(["soon", "late"]);
  });

  it("pulse derives from the soonest next fire", () => {
    const jobs = [
      job({ id: "a", next_fire: "2026-07-01T12:25:00Z" }),
      job({ id: "b", next_fire: "2026-07-03T12:00:00Z" }),
    ];
    expect(schedulePulse(jobs, now)).toBe("next in 25m");
    expect(schedulePulse([], now)).toBe("");
  });

  it("untilLabel covers now/minutes/hours/days and bad input", () => {
    expect(untilLabel("2026-07-01T12:00:30Z", now)).toBe("due now");
    expect(untilLabel("2026-07-01T11:00:00Z", now)).toBe("due now"); // past → scheduler hasn't recomputed
    expect(untilLabel("2026-07-01T12:25:00Z", now)).toBe("in 25m");
    expect(untilLabel("2026-07-01T15:00:00Z", now)).toBe("in 3h");
    expect(untilLabel("2026-07-03T12:00:00Z", now)).toBe("in 2d");
    expect(untilLabel(null, now)).toBe("");
    expect(untilLabel("not-a-date", now)).toBe("");
  });
});

describe("watch lifetime knobs (console parity with list_watches)", () => {
  const now = new Date(2026, 6, 1, 12, 0, 0).getTime();
  const inSecs = (s: number) => now / 1000 + s;

  it("formatWatchDuration matches the server's _duration bands", () => {
    expect(formatWatchDuration(45)).toBe("45s");
    expect(formatWatchDuration(600)).toBe("10m");
    expect(formatWatchDuration(7200)).toBe("2h");
    expect(formatWatchDuration(3 * 86400)).toBe("3d");
    expect(formatWatchDuration(-5)).toBe("0s"); // a just-lapsed deadline never reads negative
  });

  it("reports only the knobs that are set", () => {
    const w = watch({ status: "active", interval_s: 1800, deadline: inSecs(7200), stall_after: 3 });
    expect(watchLifetime(w, now)).toEqual(["every 30m", "expires in 2h", "stall after 3"]);
    expect(watchLifetime(watch({ status: "active" }), now)).toEqual([]);
    expect(watchLifetime(watch({ status: "active", stall_after: 2 }), now)).toEqual(["stall after 2"]);
  });

  it("says 'past its deadline' instead of counting down through zero", () => {
    const lapsed = watch({ status: "active", deadline: inSecs(-30) });
    expect(watchLifetime(lapsed, now)).toEqual(["past its deadline"]);
    expect(watchDeadlineLapsed(lapsed, now)).toBe(true);
    expect(watchDeadlineLapsed(watch({ status: "active", deadline: inSecs(60) }), now)).toBe(false);
  });

  it("stays silent on a terminal watch", () => {
    // "expires in 2h" is meaningless once a watch is met/expired — the same rule the server
    // applies before building its status line.
    for (const status of ["met", "expired"]) {
      const w = watch({ status, interval_s: 1800, deadline: inSecs(7200), stall_after: 3 });
      expect(watchLifetime(w, now)).toEqual([]);
      expect(watchDeadlineLapsed(w, now)).toBe(false);
    }
  });
});

describe("a repeating watch shows how often it has fired", () => {
  const now = new Date(2026, 6, 1, 12, 0, 0).getTime();

  it("surfaces the fire count once it is non-trivial", () => {
    // Each fire can enqueue an agent turn, so a climbing count is a cost signal.
    const w = watch({ status: "active", repeat: true, fire_count: 14 });
    expect(watchLifetime(w, now)).toContain("fired 14×");
  });

  it("stays quiet for a one-shot, or a repeater that has barely fired", () => {
    expect(watchLifetime(watch({ status: "active", fire_count: 9 }), now)).toEqual([]);
    expect(watchLifetime(watch({ status: "active", repeat: true, fire_count: 1 }), now)).toEqual(["repeating"]);
  });
});
