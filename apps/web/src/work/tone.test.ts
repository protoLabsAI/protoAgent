import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { WorkRow } from "./WorkRow";
import type { WatchState } from "../lib/types";
import { formatWatchDuration, watchLifetime, fromActivity, fromBackgroundJob, fromDecision, fromDirective, fromGoal, fromSchedule, fromTask, fromWatch, laneFor, nextTime, railCue, relTime, staleTaskMeta, statusTone, statusWord, taskStaleDays, type WorkStatus } from "./tone";
import rowCss from "./work-row.css?raw";
import { groupFor, type GroupableWorkItem, type WorkGroupId } from "./tone";

describe("groupFor raw state classification", () => {
  const cases: [GroupableWorkItem["kind"], Record<string, WorkGroupId | null>][] = [
    ["decision", { pending: "needs-you", answered: null, expired: null, cancelled: null, withdrawn: null }],
    ["goal", { active: "doing-now", exhausted: "needs-you", unachievable: "needs-you", achieved: null, cleared: null }],
    ["task", { in_progress: "doing-now", blocked: "doing-now", waiting: "needs-you", open: "coming-up", deferred: "coming-up", closed: null }],
    ["background", { running: "doing-now", open: "coming-up", deferred: "coming-up", completed: null, failed: null, canceled: null }],
    ["watch", { active: "coming-up", met: null, expired: null, cleared: null }],
    ["directive", { in_force: "instructions", fulfilled: null, revoked: null }],
    ["activity", { completed: null, failed: null, canceled: null, running: null }],
  ];
  for (const [kind, states] of cases) {
    it.each(Object.entries(states))(`${kind} %s → %s regardless of lossy status`, (state, expected) => {
      const item = { kind, id: "x", title: "x", meta: "", status: "running", raw: { status: state, state, session_id: "chat" } } as GroupableWorkItem;
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
      expect(groupFor({ ...item, kind: "delivery", raw: { delivered, needsHuman } })).toBeNull();
    }
    // Delivery notices have only Dismiss, so even operator-class pages are information.
    const page = { id: 7, priority: "now", delivered_at: null, text: "p", source: "", created_at: "", dedup_key: null } as const;
    expect(groupFor({ ...item, kind: "delivery", raw: { ...page, needs_human: true } })).toBeNull();
    expect(groupFor({ ...item, kind: "delivery", raw: { ...page } })).toBeNull();
    // Exhausting automatic retries does not invent a resolving action.
    expect(groupFor({ ...item, kind: "delivery", raw: { ...page, retries_exhausted: true } })).toBeNull();
    expect(groupFor({ ...item, kind: "delivery", raw: { ...page, retries_exhausted: true, delivered_at: "2026-09-03T10:01:00Z" } })).toBeNull();
    for (const state of ["pending", "completed", "cancelled"] as const) {
      expect(groupFor({ ...item, kind: "wait", raw: { state } })).toBe(state === "pending" ? "coming-up" : null);
    }
  });
});

const mappings: [string, (raw: string) => WorkStatus, Record<string, WorkStatus>][] = [
  ["goal", fromGoal, { active: "running", achieved: "done", exhausted: "failed", unachievable: "failed", cleared: "cancelled" }],
  ["watch", fromWatch, { active: "waiting", met: "done", expired: "cancelled", cleared: "cancelled" }],
  ["task", fromTask, { in_progress: "running", blocked: "waiting", waiting: "needs-you", open: "scheduled", deferred: "paused", closed: "done" }],
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

describe("nextTime formats a future instant in the en-GB style", () => {
  const now = new Date(2026, 10, 14, 10, 0).getTime();
  it.each([
    [new Date(2026, 10, 14, 21, 30), "at 21:30"],         // later today
    [new Date(2026, 10, 15, 14, 0), "tomorrow at 14:00"], // tomorrow
    [new Date(2026, 10, 18, 9, 0), "18 Nov at 09:00"],    // a later date
  ])("formats %s identically from seconds and ISO with relTime's tooltip shape", (date, expected) => {
    const result = nextTime(date.getTime() / 1000, now);
    expect(result).toEqual(nextTime(date.toISOString(), now));
    expect(result.text).toBe(expected);
    expect(result.tooltip).toBe(date.toLocaleString("en-GB", { dateStyle: "full", timeStyle: "long" }));
  });
  it("hides an invalid timestamp", () => {
    expect(nextTime("bad", now)).toEqual({ text: "", tooltip: "" });
  });
});

describe("the 24h quiet rule for started jobs (ADR 0129)", () => {
  const now = new Date("2026-11-12T12:00:00Z").getTime();
  const ago = (hours: number) => new Date(now - hours * 3_600_000).toISOString();

  it("flags only an in_progress job quiet past 24h, in whole days", () => {
    expect(taskStaleDays({ status: "in_progress", updated_at: ago(23) }, now)).toBeNull();
    expect(taskStaleDays({ status: "in_progress", updated_at: ago(24) }, now)).toBe(1);
    expect(taskStaleDays({ status: "in_progress", updated_at: ago(6 * 24) }, now)).toBe(6);
    // The freshest timestamp wins; creation only stands in when nothing was ever updated.
    expect(taskStaleDays({ status: "in_progress", created_at: ago(72), updated_at: ago(2) }, now)).toBeNull();
    expect(taskStaleDays({ status: "in_progress", created_at: ago(72) }, now)).toBe(3);
  });

  it("never flags other statuses or a job it cannot date", () => {
    for (const status of ["open", "blocked", "waiting", "closed", undefined]) {
      expect(taskStaleDays({ status, updated_at: ago(90) }, now)).toBeNull();
    }
    expect(taskStaleDays({ status: "in_progress" }, now)).toBeNull();
    expect(taskStaleDays({ status: "in_progress", updated_at: "bad" }, now)).toBeNull();
  });

  it("reads as plain prose with days pluralized", () => {
    expect(staleTaskMeta(1)).toBe("No activity for 1 day");
    expect(staleTaskMeta(3)).toBe("No activity for 3 days");
  });
});

describe("railCue: the one quiet line per kind (chat-status-rail-v2 §1 D2)", () => {
  const now = new Date(2026, 10, 14, 18, 0).getTime();
  const iso = (d: Date) => d.toISOString();
  const cue = (kind: GroupableWorkItem["kind"], raw: unknown) =>
    railCue({ kind, id: "x", title: "x", meta: "", status: "running", raw } as GroupableWorkItem, now);

  it("directive: 'Set ' + relTime(created_at)", () => {
    expect(cue("directive", { created_at: iso(new Date(2026, 10, 14, 14, 2)) })).toBe("Set today at 14:02");
  });

  it("background job: 'Started ' + relTime(created_at), '' without created_at", () => {
    expect(cue("background", { created_at: iso(new Date(2026, 10, 14, 17, 48)) })).toBe("Started 12 min ago");
    expect(cue("background", {})).toBe("");
  });

  it("active goal: 'Step N of M', or 'Step N' with no max", () => {
    expect(cue("goal", { iteration: 3, max_iterations: 8 })).toBe("Step 3 of 8");
    expect(cue("goal", { iteration: 3 })).toBe("Step 3");
    expect(cue("goal", {})).toBe("");
  });

  it("in-progress or blocked task: 'Updated ' + age, created_at as fallback", () => {
    expect(cue("task", { status: "in_progress", updated_at: iso(new Date(2026, 10, 14, 14, 30)) })).toBe("Updated today at 14:30");
    expect(cue("task", { status: "blocked", updated_at: iso(new Date(2026, 10, 14, 17, 55)) })).toBe("Updated 5 min ago");
    expect(cue("task", { status: "in_progress", created_at: iso(new Date(2026, 10, 14, 13, 0)) })).toBe("Updated today at 13:00");
  });

  it("a day-old in-progress task reads the staleTaskMeta line; blocked is not subject to it", () => {
    const twoDays = iso(new Date(now - 2 * 24 * 3_600_000));
    expect(cue("task", { status: "in_progress", updated_at: twoDays })).toBe("No activity for 2 days");
    expect(cue("task", { status: "blocked", updated_at: twoDays })).toBe(`Updated ${relTime(twoDays, now).text}`);
  });

  it("schedule or wait: 'Next ' + nextTime(next_fire); a paused (disabled) job reads 'Paused'", () => {
    expect(cue("schedule", { schedule: "30 21 * * *", next_fire: iso(new Date(2026, 10, 14, 21, 30)) })).toBe("Next at 21:30");
    expect(cue("wait", { schedule: "wait:x", next_fire: iso(new Date(2026, 10, 15, 14, 0)) })).toBe("Next tomorrow at 14:00");
    expect(cue("schedule", { schedule: "0 9 * * *", enabled: false, next_fire: iso(new Date(2026, 10, 18, 9, 0)) })).toBe("Paused");
  });

  it("watch: 'Since ' + relTime(created_at as epoch seconds)", () => {
    expect(cue("watch", { created_at: Math.floor(new Date(2026, 10, 12, 9, 0).getTime() / 1000) })).toBe("Since 12 Nov at 09:00");
  });

  it("open or deferred task: 'Added ' + age; a status-less task counts as open", () => {
    expect(cue("task", { status: "open", created_at: iso(new Date(2026, 10, 14, 11, 0)) })).toBe("Added today at 11:00");
    expect(cue("task", { status: "deferred", created_at: iso(new Date(2026, 10, 10, 11, 0)) })).toBe("Added 10 Nov at 11:00");
    expect(cue("task", { created_at: iso(new Date(2026, 10, 14, 11, 0)) })).toBe("Added today at 11:00");
  });

  it("returns '' when the source carries no usable timestamp or is out of scope", () => {
    expect(cue("directive", {})).toBe("");
    expect(cue("watch", {})).toBe("");
    expect(cue("task", { status: "in_progress" })).toBe("");
    expect(cue("schedule", { schedule: "x" })).toBe("");
    expect(cue("task", { status: "open" })).toBe("");
    // A waiting task and a decision are Needs-you kinds, handled in slice 3 — no cue here.
    expect(cue("task", { status: "waiting", updated_at: iso(new Date(2026, 10, 14, 17, 0)) })).toBe("");
    expect(cue("decision", { created_at: iso(new Date(2026, 10, 14, 17, 0)) })).toBe("");
  });
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
