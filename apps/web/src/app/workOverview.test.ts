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
  it("activeGoals drops achieved/failed/finished", () => {
    const goals = [
      goal({ session_id: "a" }),
      goal({ session_id: "b", status: "achieved" }),
      goal({ session_id: "c", status: "failed" }),
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

  it("visibleWatches hides cleared and floats actives first", () => {
    const ws = [
      watch({ id: "m", status: "met" }),
      watch({ id: "a", status: "active" }),
      watch({ id: "x", status: "cleared" }),
      watch({ id: "e", status: "expired" }),
    ];
    expect(visibleWatches(ws).map((w) => w.id)).toEqual(["a", "m", "e"]);
    expect(activeWatches(ws)).toHaveLength(1);
  });

  it("pulse counts watching + met-today (local day)", () => {
    const ws = [
      watch({ id: "a" }),
      watch({ id: "b" }),
      watch({ id: "m1", status: "met", last_checked: secsAt(9) }), // this morning
      watch({ id: "m2", status: "met", last_checked: secsAt(9) - 86_400 }), // yesterday
    ];
    expect(watchesPulse(ws, now)).toBe("2 watching · 1 met today");
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
