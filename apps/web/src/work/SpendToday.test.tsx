import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AnswerResult, DecisionRow } from "../lib/api/decisions";
import type { SpendCapStatus } from "../lib/api/telemetry";
import {
  SpendToday, formatCost, formatDayLine, formatReadingTime, formatSpent, formatTokens, meterValueText,
  pastNoticeLine, pausedWhy, spendState,
} from "./SpendToday";

// docs/design/work-view-additions.md §1.4 (states) and §4 (meters, focus, live region).

const spendCap = vi.hoisted(() => vi.fn());
const bus = vi.hoisted(() => ({ listeners: new Map<string, Set<() => void>>() }));
vi.mock("../lib/api", () => ({ api: { spendCap } }));
vi.mock("../lib/events", () => ({
  isConnected: () => true,
  onConnectionChange: () => () => {},
  onTopic: (topic: string, fn: () => void) => {
    const set = bus.listeners.get(topic) ?? new Set();
    bus.listeners.set(topic, set.add(fn));
    return () => set.delete(fn);
  },
}));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = Date.UTC(2026, 10, 19, 18, 0, 0);
const base: SpendCapStatus = {
  day: "2026-11-19", cost_usd: 2.1, total_tokens: 184000, enabled: true, meter_available: true,
  daily_cost_hard_limit: 10, daily_token_hard_limit: 1000000, approved_today: false, decision_id: null, paused: false,
};
const reading = (patch: Partial<SpendCapStatus> = {}): SpendCapStatus => ({ ...base, ...patch });
const noMeter = { meter_available: false, cost_usd: null, total_tokens: null };
const answered = { answered_elsewhere: false, resumed: true, decision: { id: "dec-1", state: "answered" } as DecisionRow } as AnswerResult;

let host: HTMLDivElement;
let root: Root;
const answerDecision = vi.fn<(id: string, option: string) => Promise<AnswerResult>>();
const onOpenDecision = vi.fn();

async function render() {
  await act(async () => root.render(<SpendToday answerDecision={answerDecision} onOpenDecision={onOpenDecision}
    now={() => NOW} />));
}
async function show(patch: Partial<SpendCapStatus> = {}) {
  spendCap.mockResolvedValue(reading(patch));
  await render();
}
const text = () => host.querySelector(".spend-today")!.textContent ?? "";
const badge = () => host.querySelector(".pl-badge")?.textContent;
const buttons = () => Array.from(host.querySelectorAll("button")).map((node) => node.textContent?.trim());
const button = (label: string) => Array.from(host.querySelectorAll("button")).find((node) => node.textContent?.trim() === label)!;
const meter = (kind: "cost" | "tokens") => host.querySelector<HTMLElement>(`[data-meter="${kind}"] [role="meter"]`)!;
const fillWarns = (kind: "cost" | "tokens") => !!meter(kind).querySelector(".pl-progress__fill--warning");
const live = () => host.querySelector('.spend-today [role="status"]')!.textContent;
/** The cap is off (or not yet known to be on): no block at all, nothing left behind. */
function expectNoBlock() {
  expect(host.querySelector("section.spend-today")).toBeNull();
  expect(host.querySelector("h2")).toBeNull();
  expect(host.textContent).not.toContain("Unattended spend today");
  expect(host.textContent).not.toMatch(/cap off|spend cap is off|Open Settings/i);
  expect(host.querySelector(".pl-badge")).toBeNull();
  expect(buttons()).toEqual([]);
  expect(host.textContent).toBe("");
}
const emit = (topic: string) => act(async () => bus.listeners.get(topic)?.forEach((fn) => fn()));

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  bus.listeners.clear();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("formatters", () => {
  it("show unknown for null and never throw", () => {
    expect(formatCost(6.35)).toBe("$6.35");
    expect(formatCost(1234.5)).toBe("$1,234.50");
    expect(formatTokens(541000)).toBe("541,000");
    for (const format of [formatCost, formatTokens]) {
      expect(format(null)).toBe("unknown");
      expect(format(undefined)).toBe("unknown");
      expect(format(Number.NaN)).toBe("unknown");
    }
    expect(formatSpent(3.4, 412000)).toBe("Spent so far today: $3.40 and 412,000 tokens.");
    expect(formatSpent(null, null)).toBe("Spent so far today: unknown.");
    expect(formatSpent(3.4, null)).toBe("Spent so far today: $3.40 and unknown tokens.");
  });

  it("say the day is UTC and when it resets", () => {
    expect(formatDayLine("2026-11-19", NOW)).toBe("UTC day 2026-11-19 · resets at 00:00 UTC, in 6 h");
    expect(formatDayLine("2026-11-19", Date.UTC(2026, 10, 19, 23, 35))).toBe("UTC day 2026-11-19 · resets at 00:00 UTC, in 25 min");
    expect(formatDayLine(null, NOW)).toContain("UTC day unknown");
    expect(formatReadingTime(Date.UTC(2026, 10, 19, 14, 2))).toBe("14:02 UTC");
  });

  it("write the §4 aria-valuetext, with a notice only when the response carries one", () => {
    expect(meterValueText("cost", 2.1, 10, undefined)).toBe("$2.10 of $10.00 daily limit");
    expect(meterValueText("tokens", 184000, 1000000, undefined)).toBe("184,000 of 1,000,000 tokens daily limit");
    // 0 is the setting's "no notice": still no notice anywhere.
    expect(meterValueText("cost", 2.1, 10, 0)).toBe("$2.10 of $10.00 daily limit");
    expect(meterValueText("cost", 2.1, 10, 5)).toBe("$2.10 of $10.00 daily limit, notice at $5.00");
    expect(meterValueText("cost", 6.35, 10, 5)).toBe("$6.35 of $10.00 daily limit, past the notice limit of $5.00");
    expect(meterValueText("tokens", 541000, 1000000, 600000)).toBe("541,000 of 1,000,000 tokens daily limit, notice at 600,000");
    expect(meterValueText("tokens", 700000, 1000000, 600000))
      .toBe("700,000 of 1,000,000 tokens daily limit, past the notice limit of 600,000");
    expect(meterValueText("cost", null, 10, 5)).toBe("unknown of $10.00 daily limit, notice at $5.00");
  });

  it("read the state in the order enabled, approved_today, paused, running", () => {
    expect(spendState(reading({ enabled: false, approved_today: true, paused: true }))).toBe("cap-off");
    expect(spendState(reading({ approved_today: true, paused: true }))).toBe("resumed");
    expect(spendState(reading({ paused: true }))).toBe("paused");
    expect(spendState(reading())).toBe("running");
  });

  it("explain a pause from the reading: cost, tokens, both or neither", () => {
    expect(pausedWhy(reading({ cost_usd: 10 }))).toBe("It reached the $10.00 daily limit.");
    expect(pausedWhy(reading({ total_tokens: 1000000 }))).toBe("It reached the 1,000,000-token daily limit.");
    expect(pausedWhy(reading({ cost_usd: 12, total_tokens: 1200000 })))
      .toBe("It reached the $10.00 and 1,000,000-token daily limits.");
    expect(pausedWhy(reading())).toBe("It hit a daily limit and hasn't been resumed yet.");
    expect(pausedWhy(reading(noMeter))).toBe("It hit a daily limit and hasn't been resumed yet.");
    expect(pastNoticeLine(reading({ cost_usd: 6.35, daily_cost_soft_limit: 5 })))
      .toBe("Past today's $5.00 notice limit. Unattended work keeps running until $10.00 or 1,000,000 tokens, then pauses.");
    expect(pastNoticeLine(reading({ total_tokens: 700000, daily_token_soft_limit: 600000 })))
      .toContain("Past today's 600,000-token notice limit.");
    expect(pastNoticeLine(reading({ cost_usd: 6.35 }))).toBeNull();
    expect(pastNoticeLine(reading({ ...noMeter, daily_cost_soft_limit: 5 }))).toBeNull();
  });
});

describe("states (§1.4)", () => {
  it("running: heading, UTC day line, badge and two named meters, with no notice before S1", async () => {
    await show();
    const section = host.querySelector("section.spend-today")!;
    const heading = section.querySelector("h2")!;
    expect(heading.textContent).toBe("Unattended spend today");
    expect(heading.tabIndex).toBe(-1);
    expect(section.getAttribute("aria-labelledby")).toBe(heading.id);
    expect(text()).toContain("UTC day 2026-11-19 · resets at 00:00 UTC, in 6 h");
    expect(badge()).toBe("Running");
    expect(text()).toContain("$2.10 of $10.00");
    expect(text()).toContain("184,000 of 1,000,000");
    expect(text()).toContain("Counts turns started by the scheduler, watches, the inbox, webhooks and background jobs. Your own chat turns don't count.");
    const cost = meter("cost");
    expect(document.getElementById(cost.getAttribute("aria-labelledby")!)!.textContent).toBe("Cost");
    expect(document.getElementById(meter("tokens").getAttribute("aria-labelledby")!)!.textContent).toBe("Tokens");
    expect([cost.getAttribute("aria-valuemin"), cost.getAttribute("aria-valuemax"), cost.getAttribute("aria-valuenow")])
      .toEqual(["0", "10", "2.1"]);
    expect(cost.getAttribute("aria-valuetext")).toBe("$2.10 of $10.00 daily limit");
    expect(cost.tabIndex).toBe(-1);
    expect(text()).not.toMatch(/notice/i);
    expect(host.querySelector(".spend-today__tick")).toBeNull();
    expect(fillWarns("cost") || fillWarns("tokens")).toBe(false);
    expect(buttons()).toEqual([]);
  });

  it("soft limits on the response add a notice tick and line; past one, the warning line and fill", async () => {
    await show({ cost_usd: 6.35, total_tokens: 541000, daily_cost_soft_limit: 5, daily_token_soft_limit: 600000 });
    expect(badge()).toBe("Running");
    expect(text()).toContain("Notice at $5.00 · passed");
    expect(text()).toContain("Notice at 600,000");
    expect(text()).not.toContain("Notice at 600,000 · passed");
    expect(host.querySelectorAll(".spend-today__tick")).toHaveLength(2);
    expect(host.querySelector<HTMLElement>('[data-meter="cost"] .spend-today__tick')!.style.insetInlineStart).toBe("50%");
    expect(text()).toContain("Past today's $5.00 notice limit. Unattended work keeps running until $10.00 or 1,000,000 tokens, then pauses.");
    expect(meter("cost").getAttribute("aria-valuetext")).toBe("$6.35 of $10.00 daily limit, past the notice limit of $5.00");
    expect(meter("tokens").getAttribute("aria-valuetext")).toBe("541,000 of 1,000,000 tokens daily limit, notice at 600,000");
    expect([fillWarns("cost"), fillWarns("tokens")]).toEqual([true, false]);
  });

  it("zero soft limits mean no notice anywhere", async () => {
    await show({ daily_cost_soft_limit: 0, daily_token_soft_limit: 0 });
    expect(text()).not.toMatch(/notice/i);
    expect(meter("cost").getAttribute("aria-valuetext")).not.toMatch(/notice/);
    expect(host.querySelector(".spend-today__tick")).toBeNull();
  });

  it("cap off: no block at all, with a meter", async () => {
    await show({ enabled: false, cost_usd: 3.4, total_tokens: 412000 });
    expect(spendCap).toHaveBeenCalledTimes(1);
    expectNoBlock();
  });

  it("cap off: no block at all, with no meter", async () => {
    await show({ enabled: false, ...noMeter });
    expectNoBlock();
  });

  it("cap off wins over a pause with a decision: still no block and no Resume", async () => {
    await show({ enabled: false, paused: true, approved_today: true, decision_id: "dec-1" });
    expectNoBlock();
  });

  it("nothing renders before the first reading resolves", async () => {
    let resolve!: (value: SpendCapStatus) => void;
    spendCap.mockReturnValue(new Promise<SpendCapStatus>((done) => { resolve = done; }));
    await render();
    expect(spendCap).toHaveBeenCalledTimes(1);
    expectNoBlock();
    await act(async () => resolve(reading()));
    expect(host.querySelector("section.spend-today h2")!.textContent).toBe("Unattended spend today");
    expect(badge()).toBe("Running");
  });

  it("a refresh that turns the cap off removes the block, and one that turns it on shows it again", async () => {
    await show();
    expect(badge()).toBe("Running");
    spendCap.mockResolvedValue(reading({ enabled: false }));
    await emit("decision.*");
    expectNoBlock();
    spendCap.mockResolvedValue(reading({ paused: true, decision_id: "dec-1", cost_usd: 10 }));
    await emit("decision.*");
    expect(host.querySelector("section.spend-today h2")!.textContent).toBe("Unattended spend today");
    expect(badge()).toBe("Paused");
    expect(buttons()).toEqual(["Resume unattended work for today", "Open the decision"]);
    // The block appearing is not a state change to announce.
    expect(live()).toBe("");
  });

  it("a cap-off block keeps refreshing every 60 s, so turning the cap on shows it", async () => {
    vi.useFakeTimers();
    await show({ enabled: false });
    expectNoBlock();
    spendCap.mockResolvedValue(reading());
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(spendCap).toHaveBeenCalledTimes(2);
    expect(badge()).toBe("Running");
  });

  it("a failed refresh while the cap is off stays hidden", async () => {
    await show({ enabled: false });
    spendCap.mockRejectedValueOnce(new Error("down"));
    await emit("decision.*");
    expectNoBlock();
  });

  it("paused at a limit: the callout with its why, a warning fill on that meter, Resume and Open the decision", async () => {
    await show({ paused: true, decision_id: "dec-1", cost_usd: 10.2 });
    expect(badge()).toBe("Paused");
    expect(host.querySelector(".pl-callout--warning")!.textContent).toBe(
      "Unattended work is paused for the rest of today (UTC). It reached the $10.00 daily limit. Scheduled jobs, watches, inbox and webhook turns won't start. Your own chat turns aren't affected, and job settings are unchanged.");
    expect([fillWarns("cost"), fillWarns("tokens")]).toEqual([true, false]);
    // Clamped to the max; the text carries the real value.
    expect(meter("cost").getAttribute("aria-valuenow")).toBe("10");
    expect(meter("cost").getAttribute("aria-valuetext")).toBe("$10.20 of $10.00 daily limit");
    expect(buttons()).toEqual(["Resume unattended work for today", "Open the decision"]);
    expect(button("Resume unattended work for today").className).not.toContain("primary");
    await act(async () => button("Open the decision").click());
    expect(onOpenDecision).toHaveBeenCalledWith("dec-1");
  });

  it("paused with neither limit reached never says today in its why", async () => {
    await show({ paused: true, decision_id: "dec-1" });
    expect(text()).toContain("(UTC). It hit a daily limit and hasn't been resumed yet. Scheduled jobs");
    expect([fillWarns("cost"), fillWarns("tokens")]).toEqual([false, false]);
  });

  it("paused with no decision yet: same callout and meters, no Resume", async () => {
    await show({ paused: true, total_tokens: 1000000 });
    expect(text()).toContain("It reached the 1,000,000-token daily limit.");
    expect(text()).toContain("A decision to resume appears in Needs you the next time unattended work tries to start.");
    expect(host.querySelectorAll('[role="meter"]')).toHaveLength(2);
    expect(buttons()).toEqual([]);
  });

  it("resumed for today: neutral meters that may pass 100 %", async () => {
    await show({ approved_today: true, decision_id: "dec-1", cost_usd: 14 });
    expect(badge()).toBe("Resumed for today");
    expect(text()).toContain("You resumed unattended work for today. It runs with no daily limit until 00:00 UTC, then the limits apply again.");
    expect(meter("cost").getAttribute("aria-valuenow")).toBe("10");
    expect(meter("cost").getAttribute("aria-valuetext")).toBe("$14.00 of $10.00 daily limit");
    expect(fillWarns("cost")).toBe(false);
    expect(buttons()).toEqual([]);
  });

  it("resumed with no meter", async () => {
    await show({ approved_today: true, ...noMeter });
    expect(badge()).toBe("Resumed for today");
    expect(text()).toContain("Resumed for today. Spend can't be measured right now.");
    expect(text()).toContain("Unattended work runs with no daily limit until 00:00 UTC, then the limits apply again.");
    expect(host.querySelector('[role="meter"]')).toBeNull();
  });

  it("meter unavailable: fails closed, unknown spend, Resume only once a decision exists", async () => {
    await show({ paused: true, decision_id: "dec-1", ...noMeter });
    expect(badge()).toBe("Paused");
    expect(host.querySelector(".pl-callout--warning")!.textContent).toBe(
      "Unattended work is paused: spend can't be measured right now. The cap fails closed, so nothing unattended starts until the meter is back or you resume for today.");
    expect(text()).toContain("Spent so far today: unknown.");
    expect(host.querySelector('[role="meter"]')).toBeNull();
    expect(buttons()).toEqual(["Resume unattended work for today", "Open the decision"]);

    spendCap.mockResolvedValue(reading({ paused: true, ...noMeter }));
    await emit("decision.*");
    expect(text()).toContain("A decision to resume appears in Needs you the next time unattended work tries to start.");
    expect(buttons()).toEqual([]);
  });

  it("a meter that is available but null still renders unknown", async () => {
    await show({ cost_usd: null, total_tokens: null });
    expect(text()).toContain("unknown of $10.00");
    expect(meter("tokens").getAttribute("aria-valuetext")).toBe("unknown of 1,000,000 tokens daily limit");
    expect(meter("cost").getAttribute("aria-valuenow")).toBe("0");
  });

  it("error: a failed first load says so and Retry loads it", async () => {
    spendCap.mockRejectedValueOnce(new Error("down"));
    await render();
    expect(host.querySelector(".pl-alert--warning")!.textContent)
      .toContain("Couldn't load today's spend. Unattended work still follows the cap. Only this view failed to load.");
    expect(badge()).toBeUndefined();
    spendCap.mockResolvedValue(reading());
    await act(async () => button("Retry").click());
    expect(host.querySelector(".pl-alert")).toBeNull();
    expect(badge()).toBe("Running");
  });

  it("stale: a failed refresh keeps the last reading and offers Retry", async () => {
    await show();
    spendCap.mockRejectedValueOnce(new Error("down"));
    await emit("decision.*");
    expect(host.querySelector(".pl-callout--warning")!.textContent).toContain("Showing the 18:00 UTC reading. Couldn't refresh.");
    expect(text()).toContain("$2.10 of $10.00");
    expect(badge()).toBe("Running");
    spendCap.mockResolvedValue(reading({ cost_usd: 2.5 }));
    await act(async () => button("Retry").click());
    expect(host.querySelector(".pl-callout")).toBeNull();
    expect(text()).toContain("$2.50 of $10.00");
  });
});

describe("refresh, Resume and the live region (§1.2, §4)", () => {
  it("refreshes on open, every 60 s and on decision events, and stops when closed", async () => {
    vi.useFakeTimers();
    await show();
    expect(spendCap).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(spendCap).toHaveBeenCalledTimes(2);
    await emit("decision.*");
    expect(spendCap).toHaveBeenCalledTimes(3);
    act(() => root.render(null));
    await act(async () => { await vi.advanceTimersByTimeAsync(180_000); });
    expect(spendCap).toHaveBeenCalledTimes(3);
    expect(bus.listeners.get("decision.*")?.size).toBe(0);
  });

  it("Resume answers the decision with option resume, then focus moves to the h2 and stays silent", async () => {
    await show({ paused: true, decision_id: "dec-1", cost_usd: 10 });
    answerDecision.mockResolvedValue(answered);
    spendCap.mockResolvedValue(reading({ approved_today: true, decision_id: "dec-1", cost_usd: 10 }));
    const resume = button("Resume unattended work for today");
    resume.focus();
    await act(async () => resume.click());
    expect(answerDecision).toHaveBeenCalledWith("dec-1", "resume");
    expect(badge()).toBe("Resumed for today");
    expect(buttons()).toEqual([]);
    expect(document.activeElement).toBe(host.querySelector(".spend-today h2"));
    expect(live()).toBe("");
  });

  it("a Resume whose refetch fails still reads Resumed for today", async () => {
    await show({ paused: true, decision_id: "dec-1", cost_usd: 10 });
    answerDecision.mockResolvedValue(answered);
    spendCap.mockRejectedValue(new Error("down"));
    await act(async () => button("Resume unattended work for today").click());
    expect(badge()).toBe("Resumed for today");
    expect(document.activeElement).toBe(host.querySelector(".spend-today h2"));
  });

  it("a decision already answered elsewhere refetches and shows Resumed for today", async () => {
    await show({ paused: true, decision_id: "dec-1", cost_usd: 10 });
    answerDecision.mockResolvedValue({ answered_elsewhere: true, decision: { id: "dec-1", state: "answered" } as DecisionRow });
    spendCap.mockResolvedValue(reading({ approved_today: true, decision_id: "dec-1", cost_usd: 10 }));
    await act(async () => button("Resume unattended work for today").click());
    expect(spendCap).toHaveBeenCalledTimes(2);
    expect(badge()).toBe("Resumed for today");
  });

  it("a failed Resume keeps focus on the button and shows the error", async () => {
    await show({ paused: true, decision_id: "dec-1", cost_usd: 10 });
    answerDecision.mockRejectedValue(new Error("offline"));
    const resume = button("Resume unattended work for today");
    resume.focus();
    await act(async () => resume.click());
    expect(badge()).toBe("Paused");
    expect(document.activeElement).toBe(button("Resume unattended work for today"));
    expect(host.querySelector('.spend-today [role="alert"]')!.textContent).toContain("Couldn't send your answer.");
  });

  it("the live region speaks on a state change from elsewhere, never on a meter refresh", async () => {
    await show();
    expect(live()).toBe("");
    spendCap.mockResolvedValue(reading({ cost_usd: 4 }));
    await emit("decision.*");
    expect(text()).toContain("$4.00 of $10.00");
    expect(live()).toBe("");
    spendCap.mockResolvedValue(reading({ paused: true, decision_id: "dec-1", cost_usd: 10 }));
    await emit("decision.*");
    expect(live()).toBe("Unattended work is paused.");
    spendCap.mockResolvedValue(reading({ approved_today: true, decision_id: "dec-1", cost_usd: 10 }));
    await emit("decision.*");
    expect(live()).toBe("Unattended work resumed for today.");
    const region = host.querySelector('.spend-today [role="status"]')!;
    expect([region.getAttribute("aria-live"), region.getAttribute("aria-atomic")]).toEqual(["polite", "true"]);
  });
});
