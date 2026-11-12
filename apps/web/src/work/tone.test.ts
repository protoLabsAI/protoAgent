import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { WorkRow } from "./WorkRow";
import type { WatchState } from "../lib/types";
import { formatWatchDuration, watchLifetime, fromActivity, fromBackgroundJob, fromDecision, fromDirective, fromGoal, fromSchedule, fromTask, fromWatch, laneFor, relTime, statusTone, statusWord, type WorkStatus } from "./tone";
import rowCss from "./work-row.css?raw";
import { groupFor, type GroupableWorkItem, type WorkGroupId } from "./tone";

describe("groupFor raw state classification", () => {
  const cases: [GroupableWorkItem["kind"], Record<string, WorkGroupId | null>][] = [
    ["decision", { pending: "needs-you", answered: null, expired: null, cancelled: null, withdrawn: null }],
    ["goal", { active: "doing-now", exhausted: "needs-you", unachievable: "needs-you", achieved: null, cleared: null }],
    ["task", { in_progress: "doing-now", blocked: "doing-now", open: "coming-up", deferred: "coming-up", closed: null }],
    ["background", { running: "doing-now", open: "coming-up", deferred: "coming-up", completed: null, failed: null, canceled: null }],
    ["watch", { active: "coming-up", met: null, expired: null, cleared: null }],
    ["directive", { in_force: "instructions", fulfilled: null, revoked: null }],
    ["activity", { completed: null, failed: null, canceled: null, running: null }],
  ];
  for (const [kind, states] of cases) {
    it.each(Object.entries(states))(`${kind} %s → %s regardless of lossy status`, (state, expected) => {
      const item = { kind, id: "x", title: "x", meta: "", status: "running", raw: { status: state, state } } as GroupableWorkItem;
      expect(groupFor(item)).toBe(expected);
    });
  }
  it("counts a blocked task as started unless the adapter says otherwise", () => {
    // The task API carries no start history, so `blocked` (someone acted on it) is the
    // evidence of a started job — the production shape, with no `started` flag, stays in
    // Doing now. Only an adapter that KNOWS it never started (`started: false`) demotes it.
    const task: GroupableWorkItem = { kind: "task", id: "t", title: "t", meta: "", status: "waiting", raw: { status: "blocked" } };
    expect(groupFor(task)).toBe("doing-now");
    expect(groupFor({ ...task, started: true })).toBe("doing-now");
    expect(groupFor({ ...task, started: false })).toBe("coming-up");
    expect(groupFor({ ...task, raw: { status: "open" }, started: true })).toBe("coming-up");
    expect(groupFor({ ...task, raw: {} })).toBe("coming-up");
    expect(groupFor({ ...task, raw: { status: "future-status" } })).toBeNull();
  });
  it("keeps repeat watches armed and excludes completed one-shot schedules", () => {
    const item = { id: "x", title: "x", meta: "", status: "waiting" as const };
    expect(groupFor({ ...item, kind: "watch", raw: { status: "met", repeat: true } })).toBe("coming-up");
    for (const job of [
      { schedule: "0 9 * * *", enabled: true, next_fire: "2026-11-15T09:00:00Z" },
      { schedule: "0 9 * * *", enabled: false },
      { schedule: "2026-11-07T09:00:00Z", last_fire: "2026-11-07T09:00:00Z" },
    ]) {
      const status = fromSchedule(job);
      expect(groupFor({ ...item, kind: "schedule", raw: job, status })).toBe(status === "done" ? null : "coming-up");
    }
  });
  it("accepts typed delivery and wait adapters", () => {
    const item = { id: "x", title: "x", meta: "", status: "waiting" as const };
    for (const delivered of [true, false]) for (const needsHuman of [true, false]) {
      expect(groupFor({ ...item, kind: "delivery", raw: { delivered, needsHuman } })).toBe(!delivered && needsHuman ? "needs-you" : null);
    }
    for (const state of ["pending", "completed", "cancelled"] as const) {
      expect(groupFor({ ...item, kind: "wait", raw: { state } })).toBe(state === "pending" ? "coming-up" : null);
    }
  });
});

const mappings: [string, (raw: string) => WorkStatus, Record<string, WorkStatus>][] = [
  ["goal", fromGoal, { active: "running", achieved: "done", exhausted: "failed", unachievable: "failed", cleared: "cancelled" }],
  ["watch", fromWatch, { active: "waiting", met: "done", expired: "cancelled", cleared: "cancelled" }],
  ["task", fromTask, { in_progress: "running", blocked: "waiting", open: "scheduled", deferred: "paused", closed: "done" }],
  ["background", fromBackgroundJob, { running: "running", completed: "done", failed: "failed", canceled: "cancelled" }],
  ["activity", fromActivity, { completed: "done", failed: "failed", canceled: "cancelled" }],
  ["decision", fromDecision, { pending: "needs-you", answered: "done", expired: "cancelled", cancelled: "cancelled", withdrawn: "cancelled" }],
  ["directive", fromDirective, { in_force: "waiting", fulfilled: "done", revoked: "cancelled" }],
];

describe.each(mappings)("%s raw statuses", (_kind, mapper, cases) => {
  it.each(Object.entries(cases))("%s maps to %s", (raw, expected) => {
    expect(mapper(raw)).toBe(expected);
  });
  it("falls back and warns once for each unknown raw value in dev", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(mapper("unknown-test")).toBe("waiting");
      expect(mapper("unknown-test")).toBe("waiting");
      expect(warn).toHaveBeenCalledTimes(1);
      expect(mapper("constructor")).toBe("waiting");
    } finally { warn.mockRestore(); }
  });
});

it("distinguishes verifier waits and repeat watches", () => {
  expect(fromGoal("active", false)).toBe("waiting");
  expect(fromWatch("met", true)).toBe("waiting");
  expect(fromWatch("expired", true)).toBe("cancelled");
});

it.each([
  [{ schedule: "0 9 * * *", enabled: true, next_fire: "2026-11-10T09:00:00Z" }, "scheduled"],
  [{ schedule: "0 9 * * *", enabled: false, next_fire: "2026-11-10T09:00:00Z" }, "paused"],
  [{ schedule: "2026-11-07T09:00:00Z", enabled: false, last_fire: "2026-11-07T09:00:00Z" }, "done"],
  [{ schedule: "2026-11-07 09:00:00", last_fire: "2026-11-07T09:00:00Z" }, "done"],
  [{ schedule: "0 9 * * *", last_fire: "2026-11-07T09:00:00Z", next_fire: "2026-11-10T09:00:00Z" }, "scheduled"],
] as const)("maps schedule %j", (job, expected) => {
  expect(fromSchedule(job)).toBe(expected);
});
it("falls back for an unrecognised schedule state", () => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  try {
    expect(fromSchedule({ schedule: "unknown" })).toBe("waiting");
    expect(fromSchedule({ schedule: "unknown" })).toBe("waiting");
    expect(warn).toHaveBeenCalledTimes(1);
  } finally { warn.mockRestore(); }
});

const statuses = [
  ["needs-you", "warning", "needs you", "needs-you"], ["running", "info", "running", "doing"],
  ["waiting", "neutral", "waiting", "waiting"], ["scheduled", "neutral", "scheduled", "next"],
  ["paused", "neutral", "paused", "next"], ["done", "success", "done", "done"],
  ["failed", "error", "failed", "done"], ["cancelled", "neutral", "cancelled", "done"],
] as const;
it.each(statuses)("gives %s its tone, word and lane", (status, tone, word, lane) => {
  expect(statusTone(status)).toBe(tone);
  expect(statusWord(status)).toBe(word);
  expect(laneFor(status)).toBe(lane);
});

it.each([
  [new Date(2026, 10, 9, 18, 12), "2 min ago"],
  [new Date(2026, 10, 9, 17, 14), "today at 17:14"],
  [new Date(2026, 10, 7, 17, 14), "7 Nov at 17:14"],
])("formats %s identically from seconds and ISO", (date, expected) => {
  const now = new Date(2026, 10, 9, 18, 14).getTime();
  const result = relTime(date.getTime() / 1000, now);
  expect(result).toEqual(relTime(date.toISOString(), now));
  expect(result.text).toBe(expected);
  expect(result.tooltip).toBe(date.toLocaleString("en-GB", { dateStyle: "full", timeStyle: "long" }));
});
it("hides invalid timestamps and does not describe future times as ago", () => {
  expect(relTime("bad")).toEqual({ text: "", tooltip: "" });
  expect(relTime(new Date(2026, 10, 9, 19, 14).toISOString(), new Date(2026, 10, 9, 18, 14).getTime()).text).toBe("today at 19:14");
});

it.each(statuses)("renders one accessible %s row", (status, tone, word) => {
  const container = document.createElement("div");
  const root = createRoot(container);
  const onOpen = vi.fn();
  try {
    act(() => root.render(createElement(WorkRow, { status, title: "A work item", meta: "Updated today", onOpen, focusKey: "current" })));
    const button = container.querySelector("button")!;
    expect(container.querySelectorAll("button")).toHaveLength(1);
    expect(button.type).toBe("button");
    expect(button.querySelector("button, a, input, select, textarea, [tabindex], [role=button]")).toBeNull();
    expect(button.dataset.surfaceFocus).toBe("current");
    expect(button.querySelector(".pl-badge")?.textContent).toBe(word);
    expect(button.querySelector(".pl-dot")).not.toBeNull();
    expect(!!button.querySelector(".pl-dot--pulse")).toBe(status === "running");
    if (tone !== "neutral") expect(button.querySelector(`.pl-dot--${tone}`)).not.toBeNull();
    expect(button.querySelector(".work-row__meta")?.textContent).toBe("Updated today");
    act(() => button.click());
    expect(onOpen).toHaveBeenCalledOnce();
    act(() => root.render(createElement(WorkRow, { status, title: "A work item", meta: "", onOpen })));
    expect(button.hasAttribute("data-surface-focus")).toBe(false);
    expect(button.querySelector(".work-row__meta")).toBeNull();
  } finally { act(() => root.unmount()); }
});
it("uses muted meta, clamped titles and no status text colour", () => {
  expect(rowCss).toMatch(/\.work-row__meta\s*\{[^}]*color: var\(--pl-color-fg-muted\)/);
  expect(rowCss).toContain("-webkit-line-clamp: 2");
  expect(rowCss).not.toMatch(/fg-subtle|color-status|\dpx/);
});

const watch = (over: Partial<WatchState>): WatchState => ({
  id: "w",
  condition: "c",
  status: "active",
  ...over,
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
  });

  it("stays silent on a terminal watch", () => {
    // "expires in 2h" is meaningless once a watch is met/expired — the same rule the server
    // applies before building its status line.
    for (const status of ["met", "expired"]) {
      const w = watch({ status, interval_s: 1800, deadline: inSecs(7200), stall_after: 3 });
      expect(watchLifetime(w, now)).toEqual([]);
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
