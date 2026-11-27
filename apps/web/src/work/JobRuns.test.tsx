// A scheduled job's Recent runs list (work-view-additions §2.3–§2.4, #5543), mounted for real
// inside a QueryClientProvider. Only the `api` slice is stubbed: every §2.3 row, the one
// clock, Show older runs and each §2.4 state are read back off the DOM.

import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({ scheduleRuns: vi.fn() }));
vi.mock("../lib/api", () => ({ api: apiMock, currentSlug: () => "test" }));

import { JobRuns, runDuration, runPresentation } from "./JobRuns";
import type { ScheduledJob, ScheduledJobRun } from "../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CREATED = "2026-11-01T08:00:00+00:00";
function job(over: Partial<ScheduledJob> = {}): ScheduledJob {
  return {
    id: "job-7", prompt: "Post the standup", schedule: "0 14 * * *", enabled: true, timezone: null,
    created_at: CREATED, next_fire: "2026-11-21T14:00:00+00:00", last_fire: "2026-11-20T14:00:00+00:00", ...over,
  };
}
function run(over: Partial<ScheduledJobRun> = {}): ScheduledJobRun {
  return {
    job_id: "job-7", scheduled_for: "2026-11-20T14:00:00+00:00", state: "settled", task_state: "completed",
    ok: true, error: null, task_id: "task-1", message_id: "msg-1", job_created_at: CREATED, ...over,
  };
}
/** `count` distinct settled runs, newest first. */
function page(count: number): { job_id: string; runs: ScheduledJobRun[] } {
  return { job_id: "job-7", runs: Array.from({ length: count }, (_, i) => run({
    scheduled_for: new Date(Date.UTC(2026, 10, 19, 14) - i * 86_400_000).toISOString(), task_id: `task-${i}`,
  })) };
}
function httpError(status: number): Error {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

let root: Root | null = null;
let host: HTMLDivElement | null = null;

async function mount(target: ScheduledJob) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const render = (next: ScheduledJob) => act(async () => root!.render(h(QueryClientProvider, { client }, h(JobRuns, { job: next }))));
  await render(target);
  await settle();
  return render;
}
/** Let the query's macrotask-scheduled notifications land (timers are real; only Date is pinned). */
async function settle() {
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
function rows(): HTMLLIElement[] {
  return Array.from(host!.querySelectorAll<HTMLLIElement>("ul > li"));
}
function button(text: string): HTMLButtonElement | undefined {
  return Array.from(host!.querySelectorAll("button")).find((b) => b.textContent?.trim() === text);
}
async function click(text: string) {
  await act(async () => button(text)!.click());
  await settle();
}

beforeEach(() => {
  vi.clearAllMocks();
  // Only the clock is pinned ("today" is 20 Nov 2026 in every zone used here); timers stay real.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-20T15:00:00Z"));
});
afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.useRealTimers();
});

describe("the §2.3 run table", () => {
  const TABLE: [string, Partial<ScheduledJobRun>, string, string, string][] = [
    ["settled ok", { state: "settled", ok: true }, "Ran", "success", "Completed"],
    ["settled failed", { state: "settled", ok: false, task_state: "failed", error: "Gateway timed out" }, "Failed", "warning", "Gateway timed out"],
    ["running", { state: "running", ok: null, task_state: null }, "Running", "info", "Started, still going."],
    ["claimed", { state: "claimed", ok: null, task_state: null }, "Not confirmed", "neutral", "It was due, but there's no record it started."],
    ["unknown", { state: "unknown", ok: null, task_state: null }, "Not confirmed", "neutral", "It started, but the result never came back. It may still finish."],
    ["retryable", { state: "retryable", ok: null, task_state: null }, "Not run", "neutral", "It didn't start. This slot is still owed, so it will try again."],
    ["interrupted", { state: "interrupted", ok: null, task_state: null }, "Skipped", "neutral", "A restart interrupted it."],
    ["interrupted_needs_operator", { state: "interrupted_needs_operator", ok: null, task_state: null }, "Waiting on you", "neutral", "A restart interrupted it. It's in Needs you."],
    ["canceled", { state: "settled", ok: null, task_state: "canceled" }, "Canceled", "neutral", "It was canceled before it finished."],
    ["cancelled", { state: "settled", ok: null, task_state: "cancelled" }, "Canceled", "neutral", "It was canceled before it finished."],
    ["settled, outcome unrecorded", { state: "settled", ok: null, task_state: null }, "Ran", "neutral", "Outcome not recorded."],
    ["settled, other task_state", { state: "settled", ok: null, task_state: "input-required" }, "Ran", "neutral", "Outcome not recorded."],
  ];

  it.each(TABLE)("%s", (_name, over, word, tone, note) => {
    expect(runPresentation(run(over))).toEqual({ word, tone, note });
  });

  it("draws one li per run under the h3, newest first: word, dot tone, job-clock time and second line", async () => {
    apiMock.scheduleRuns.mockResolvedValue({ job_id: "job-7", runs: TABLE.map(([, over], i) => run({
      ...over, scheduled_for: new Date(Date.UTC(2026, 10, 20, 14) - i * 3_600_000).toISOString(),
    })) });
    await mount(job());
    expect(apiMock.scheduleRuns).toHaveBeenCalledWith("job-7", 20);
    const heading = host!.querySelector("h3")!;
    expect(heading.textContent).toBe("Recent runs");
    expect(host!.querySelector("ul")!.getAttribute("aria-labelledby")).toBe(heading.id);
    expect(rows()).toHaveLength(TABLE.length);
    expect(host!.querySelectorAll("ul button.work-row")).toHaveLength(0); // rows are not buttons
    rows().forEach((row, i) => {
      const [, , word, tone, note] = TABLE[i];
      expect(row.querySelector(".job-run__word")!.textContent).toBe(word);
      const dot = row.querySelector(".pl-dot")!;
      expect(dot.getAttribute("aria-hidden")).not.toBeNull();
      expect(dot.classList.contains(`pl-dot--${tone}`)).toBe(tone !== "neutral");
      expect(dot.className).not.toContain("pl-dot--error");
      expect(row.querySelector(".job-run__note")!.textContent).toBe(note);
    });
    expect(rows()[0].querySelector(".pl-chip-meta")!.textContent).toBe("Today 14:00 UTC");
    expect(rows()[1].querySelector(".pl-chip-meta")!.textContent).toBe("Today 13:00 UTC");
  });

  it("uses the job's timezone for every run time and names the zone; an earlier day reads as a date", async () => {
    apiMock.scheduleRuns.mockResolvedValue({ job_id: "job-7", runs: [
      run(), run({ scheduled_for: "2026-11-16T23:30:00+00:00" }),
    ] });
    await mount(job({ timezone: "Europe/Berlin" }));
    expect(rows()[0].querySelector(".pl-chip-meta")!.textContent).toBe("Today 15:00 Europe/Berlin");
    expect(rows()[1].querySelector(".pl-chip-meta")!.textContent).toBe("Nov 17 00:30 Europe/Berlin");
  });

  it("each run has its own collapsed Technical details with the six ledger keys", async () => {
    apiMock.scheduleRuns.mockResolvedValue({ job_id: "job-7", runs: [run()] });
    await mount(job());
    expect(host!.textContent).not.toContain("msg-1");
    await click("Technical details");
    const terms = Array.from(rows()[0].querySelectorAll("dt")).map((dt) => dt.textContent);
    expect(terms).toEqual(["state", "task_state", "scheduled_for", "task_id", "message_id", "job_created_at"]);
    expect(rows()[0].textContent).toContain("2026-11-20T14:00:00+00:00");
    expect(rows()[0].textContent).toContain("msg-1");
  });

  it("each run's compact Technical details opens on its own, its ledger scoped to that run", async () => {
    apiMock.scheduleRuns.mockResolvedValue({ job_id: "job-7", runs: [run(), run({ task_id: "task-2", message_id: "msg-2" })] });
    await mount(job());
    const toggles = rows().map((li) => li.querySelector<HTMLButtonElement>(".technical-details__toggle")!);
    expect(toggles.map((t) => t.getAttribute("aria-expanded"))).toEqual(["false", "false"]);
    expect(host!.querySelector(".pl-accordion")).toBeNull();
    await act(async () => toggles[1].click());
    expect(toggles.map((t) => t.getAttribute("aria-expanded"))).toEqual(["false", "true"]);
    expect(rows()[0].textContent).not.toContain("msg-1");
    expect(rows()[1].textContent).toContain("msg-2");
    expect(rows()[1].textContent).not.toContain("msg-1");
  });

  it("marks a run from an older generation of the id on line 2", async () => {
    apiMock.scheduleRuns.mockResolvedValue({ job_id: "job-7", runs: [
      run(), run({ job_created_at: "2026-10-01T08:00:00+00:00" }),
    ] });
    await mount(job());
    expect(rows()[0].querySelector(".job-run__note")!.textContent).toBe("Completed");
    expect(rows()[1].querySelector(".job-run__note")!.textContent).toBe("Completed · From an earlier version of this job");
  });

  it("shows the duration and 'stayed silent' when S2 fields are present, nothing extra when null", async () => {
    apiMock.scheduleRuns.mockResolvedValue({ job_id: "job-7", runs: [
      run({ started_at: "2026-11-20T14:00:01+00:00", ended_at: "2026-11-20T14:01:06+00:00", duration_s: 65, reported: false }),
      run({ duration_s: 42.4, reported: true }),
      run({ started_at: null, ended_at: null, duration_s: null, reported: null }),
    ] });
    await mount(job());
    const chips = (row: HTMLLIElement) => Array.from(row.querySelectorAll(".pl-chip-meta")).map((c) => c.textContent);
    expect(chips(rows()[0])).toEqual(["Today 14:00 UTC", "1 min 5 s"]);
    expect(rows()[0].querySelector(".job-run__note")!.textContent).toBe("Completed · stayed silent");
    expect(chips(rows()[1])).toEqual(["Today 14:00 UTC", "42 s"]);
    expect(rows()[1].querySelector(".job-run__note")!.textContent).toBe("Completed");
    expect(chips(rows()[2])).toEqual(["Today 14:00 UTC"]);
    expect(rows()[2].querySelector(".job-run__note")!.textContent).toBe("Completed");
  });

  it("formats durations in plain units", () => {
    expect([0.2, 42, 60, 185, 3600, 4320].map(runDuration)).toEqual(["under 1 s", "42 s", "1 min", "3 min 5 s", "1 h", "1 h 12 min"]);
    expect([null, undefined, -1, Number.NaN].map(runDuration)).toEqual(["", "", "", ""]);
  });
});

describe("Show older runs", () => {
  it("appears only when the page came back full, re-fetches with limit=200, then disappears", async () => {
    apiMock.scheduleRuns.mockResolvedValueOnce(page(20)).mockResolvedValueOnce(page(57));
    await mount(job());
    expect(rows()).toHaveLength(20);
    await click("Show older runs");
    expect(apiMock.scheduleRuns).toHaveBeenLastCalledWith("job-7", 200);
    expect(rows()).toHaveLength(57);
    expect(button("Show older runs")).toBeUndefined();
    expect(host!.textContent).not.toContain("Showing the latest 200 runs.");
  });

  it("is absent when fewer runs than the limit came back", async () => {
    apiMock.scheduleRuns.mockResolvedValue(page(19));
    await mount(job());
    expect(rows()).toHaveLength(19);
    expect(button("Show older runs")).toBeUndefined();
  });

  it("says it shows the latest 200 when the larger page came back full too", async () => {
    apiMock.scheduleRuns.mockResolvedValueOnce(page(20)).mockResolvedValueOnce(page(200));
    await mount(job());
    await click("Show older runs");
    expect(rows()).toHaveLength(200);
    expect(button("Show older runs")).toBeUndefined();
    expect(host!.textContent).toContain("Showing the latest 200 runs.");
  });

  it("a failed larger page keeps the list under a stale notice whose Retry re-asks for 200", async () => {
    apiMock.scheduleRuns.mockResolvedValueOnce(page(20)).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(page(30));
    await mount(job());
    await click("Show older runs");
    expect(rows()).toHaveLength(20);
    expect(host!.querySelector(".pl-callout")!.textContent).toContain("Showing the last load. Couldn't refresh.");
    expect(button("Show older runs")).toBeUndefined();
    await click("Retry");
    expect(apiMock.scheduleRuns).toHaveBeenLastCalledWith("job-7", 200);
    expect(rows()).toHaveLength(30);
    expect(host!.querySelector(".pl-callout")).toBeNull();
  });
});

describe("the §2.4 empty and unsupported states", () => {
  it("no runs, job enabled: says when it first runs, in the job's clock", async () => {
    apiMock.scheduleRuns.mockResolvedValue({ job_id: "job-7", runs: [] });
    await mount(job({ timezone: "Europe/Berlin" }));
    expect(host!.querySelector("h3")!.textContent).toBe("Recent runs");
    expect(host!.querySelector("ul")).toBeNull();
    expect(host!.textContent).toContain("No runs yet. It first runs Nov 21 15:00 Europe/Berlin.");
  });

  it("no runs, job turned off", async () => {
    apiMock.scheduleRuns.mockResolvedValue({ job_id: "job-7", runs: [] });
    await mount(job({ enabled: false, next_fire: null }));
    expect(host!.textContent).toContain("No runs yet. It's turned off, so it won't run until you turn it on.");
  });

  it("404: the job is gone and left no runs — no Retry", async () => {
    apiMock.scheduleRuns.mockRejectedValue(httpError(404));
    await mount(job());
    expect(host!.textContent).toContain("This job is gone and left no runs.");
    expect(button("Retry")).toBeUndefined();
  });

  it("501: run history isn't available — no Retry", async () => {
    apiMock.scheduleRuns.mockRejectedValue(httpError(501));
    await mount(job());
    expect(host!.textContent).toContain("Run history isn't available. This scheduler backend doesn't keep run history.");
    expect(button("Retry")).toBeUndefined();
  });

  it.each([["a network failure", new TypeError("Failed to fetch")], ["a 5xx", httpError(503)]])(
    "%s: the load error with a Retry that loads the list",
    async (_name, error) => {
      apiMock.scheduleRuns.mockRejectedValueOnce(error).mockResolvedValueOnce(page(2));
      await mount(job());
      expect(host!.textContent).toContain("Couldn't load the run history. The job itself is fine. Only this list failed to load.");
      await click("Retry");
      expect(apiMock.scheduleRuns).toHaveBeenLastCalledWith("job-7", 20);
      expect(rows()).toHaveLength(2);
    },
  );

  it("a refresh that fails after a good load keeps the list under a stale Callout with Retry", async () => {
    apiMock.scheduleRuns.mockResolvedValueOnce(page(3)).mockRejectedValueOnce(httpError(502)).mockResolvedValueOnce(page(4));
    const render = await mount(job());
    expect(rows()).toHaveLength(3);
    // The job recorded another fire while its detail stayed open — the list refreshes.
    await render(job({ last_fire: "2026-11-20T14:30:00+00:00" }));
    await settle();
    expect(apiMock.scheduleRuns).toHaveBeenCalledTimes(2);
    expect(rows()).toHaveLength(3);
    expect(host!.querySelector(".pl-callout")!.textContent).toContain("Showing the last load. Couldn't refresh.");
    await click("Retry");
    expect(rows()).toHaveLength(4);
    expect(host!.querySelector(".pl-callout")).toBeNull();
  });
});
