import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { WorkRow } from "./WorkRow";
import { fromActivity, fromBackgroundJob, fromDecision, fromGoal, fromSchedule, fromTask, fromWatch, laneFor, relTime, statusTone, statusWord, type WorkStatus } from "./tone";
import rowCss from "./work-row.css?raw";

const mappings: [string, (raw: string) => WorkStatus, Record<string, WorkStatus>][] = [
  ["goal", fromGoal, { active: "running", achieved: "done", exhausted: "failed", unachievable: "failed", cleared: "cancelled" }],
  ["watch", fromWatch, { active: "waiting", met: "done", expired: "cancelled", cleared: "cancelled" }],
  ["task", fromTask, { in_progress: "running", blocked: "waiting", open: "scheduled", deferred: "paused", closed: "done" }],
  ["background", fromBackgroundJob, { running: "running", completed: "done", failed: "failed", canceled: "cancelled" }],
  ["activity", fromActivity, { completed: "done", failed: "failed", canceled: "cancelled" }],
  ["decision", fromDecision, { pending: "needs-you", answered: "done", expired: "cancelled", cancelled: "cancelled", withdrawn: "cancelled" }],
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
