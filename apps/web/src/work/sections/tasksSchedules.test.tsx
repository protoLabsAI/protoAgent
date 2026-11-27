// The Tasks and Schedules Work sections (#4955 §2.7–2.8), mounted for real inside the DS
// ToastProvider (so the undo toast renders) and a QueryClientProvider (so the detail's
// mutations/invalidations work). Only the `api` slice is stubbed — rows, detail fields,
// Pause/Resume, Delete and Undo are all driven through the DOM.

import { createElement as h, type ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConversationRow } from "../../lib/api/chat";
import { __resetForTest as resetConversationsStore, snapshotForTest as conversationsSnapshot } from "../../chat/conversations-store";

const apiMock = vi.hoisted(() => ({
  setScheduleEnabled: vi.fn(async () => ({ job: {} })),
  cancelSchedule: vi.fn(async () => ({ canceled: true, undo_token: "tok-sched" })),
  updateSchedule: vi.fn(async () => ({ job: {} })),
  // The schedule detail's Recent runs list (work-view-additions §2.3); empty unless a case sets rows.
  scheduleRuns: vi.fn(async () => ({ job_id: "job-7", runs: [] as unknown[] })),
  deleteTask: vi.fn(async () => ({ deleted: "task-3", undo_token: "tok-task" })),
  updateTask: vi.fn(async () => ({ issue: {} })),
  closeTask: vi.fn(async () => ({ issue: {} })),
  undo: vi.fn(async () => ({ restored: true, kind: "task" })),
  // The waiting-task detail reads turns to tell a live source from a deleted one, and lists
  // conversations to name a thread's caller for the §4.6 provenance link.
  chatSessionTurns: vi.fn(async () => ({ turns: [] as unknown[] })),
  listConversations: vi.fn(async () => ({ conversations: [] as ConversationRow[] })),
  // The waiting-task reply (#5400): a steer into the source, or a fresh chat registered for it.
  steerChat: vi.fn(async (..._args: unknown[]) => ({ ok: true, id: "reply", pending: 0, started_turn: "turn-2" as string | null })),
  registerSession: vi.fn(async () => ({ ok: true, session_id: "new" })),
}));
vi.mock("../../lib/api", () => ({ api: apiMock, currentSlug: () => "test" }));

// The "Started in" A2A link routes through the one palette navigate chokepoint; spy on it to
// prove the click, keeping the rest of the module real.
const navigateMock = vi.hoisted(() => vi.fn());
vi.mock("../../app/palette/nav", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/palette/nav")>()),
  navigate: navigateMock,
}));

import { SchedulesSection } from "./schedules";
import { TasksSection } from "./tasks";
import { chatStore } from "../../chat/chat-store";
import { buildOnce } from "../../schedule/schedule-builder";
import type { WorkItem, WorkItems, WorkSource } from "../useWorkItems";
import type { WorkSectionContext } from "../WorkView";
import { sections } from "../sectionRegistry";
import { workGroups } from "../workGroups";
import { groupFor, type GroupableWorkItem } from "../tone";
import { WorkGroupRows } from "../WorkOverview";
import { scheduleMeta } from "../rowMeta";
import { parseAskRefs } from "../askRefs";
import { ARTIFACT_VIEW_KEY } from "../../artifacts/artifactRef";
import { resetPluginViewInbox, takePluginViewMessages } from "../../lib/pluginViewInbox";
import { useUI } from "../../state/uiStore";
import { ApiError } from "../../lib/api/http";
import { PRIVATE_STEER_WAITING_ON_PEER } from "../../chat/privateSteer";

vi.mock("../../schedule/schedule-builder", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../schedule/schedule-builder")>(),
  localZone: () => "America/Los_Angeles",
}));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("annotates tasks without overriding per-row goal/task/watch classification", () => {
  expect(sections.get("tasks")?.group).toBe("doing-now");
  expect(sections.get("schedules")?.group).toBe("coming-up");
  const base = { id: "item", title: "Item", meta: "", status: "waiting" as const };
  const groups = workGroups([
    { ...base, id: "active", kind: "goal", raw: { status: "active" } },
    { ...base, id: "stopped", kind: "goal", raw: { status: "exhausted", session_id: "stopped" } },
    { ...base, id: "achieved", kind: "goal", raw: { status: "achieved" } },
    { ...base, id: "running", kind: "task", raw: { status: "in_progress" } },
    { ...base, id: "queued", kind: "task", raw: { status: "open" } },
    { ...base, id: "parked", kind: "task", raw: { status: "waiting" } },
    { ...base, id: "closed", kind: "task", raw: { status: "closed" } },
    { ...base, id: "watch", kind: "watch", raw: { status: "active" } },
  ]);
  expect(groups.map(({ id, items }) => [id, items.map((item) => item.id)])).toEqual([
    ["needs-you", ["stopped", "parked"]],
    ["doing-now", ["active", "running"]],
    ["coming-up", ["queued", "watch"]],
    ["instructions", []],
  ]);
});

// WorkView's heading counts, badges and announcement come from the shared workGroups
// selector alone (the test above) — sections no longer register a listFilter for them.

let root: Root | null = null;
let host: HTMLDivElement | null = null;

const KINDS: (keyof WorkItems)[] = [
  "decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives",
];
function src(items: WorkItem[]): WorkSource {
  return { items, status: "ready", loadedAt: 1, error: null, retry: vi.fn() };
}
function sources(over: Partial<WorkItems>): WorkItems {
  const base = Object.fromEntries(KINDS.map((k) => [k, src([])])) as WorkItems;
  return { ...base, ...over };
}
function ctx(over: Partial<WorkSectionContext>): WorkSectionContext {
  return { sources: sources({}), state: {}, onOpen: vi.fn(), onBack: vi.fn(), ...over };
}

function scheduleItem(over: Partial<ScheduledJobRaw> = {}): WorkItem {
  const raw: ScheduledJobRaw = {
    id: "job-7",
    prompt: "Post the weekly standup\nand @-mention the team",
    schedule: "0 9 * * 1-5",
    next_fire: "2099-01-01T09:00:00Z",
    last_fire: "2026-11-07T09:00:00Z",
    enabled: true,
    timezone: null,
    created_at: "2026-11-07T09:00:00Z",
    ...over,
  };
  return {
    kind: "schedule",
    id: raw.id,
    status: raw.enabled === false ? "paused" : "scheduled",
    title: raw.prompt,
    meta: raw.schedule,
    raw,
  };
}
type ScheduledJobRaw = {
  id: string; prompt: string; schedule: string;
  next_fire: string | null; last_fire: string | null;
  enabled: boolean; timezone: string | null; created_at: string;
  consecutive_failures?: number; last_error?: string | null; last_ok?: string | null;
};

function taskItem(over: Partial<TaskRaw> = {}): WorkItem {
  const raw: TaskRaw = {
    id: "task-3",
    title: "Fix the flaky login test",
    status: "in_progress",
    type: "bug",
    priority: 1,
    assignee: "casey",
    description: "Steps to reproduce are in the ticket.",
    created_at: "2026-11-07T09:00:00Z",
    // Fresh by default (tests run on the real clock): the 24h stale rule is opted into
    // per test with an explicitly old update.
    updated_at: new Date(Date.now() - 3_600_000).toISOString(),
    closed_at: null,
    ...over,
  };
  return { kind: "task", id: raw.id, status: "running", title: raw.title, meta: raw.type, raw };
}
type TaskRaw = {
  id: string; title: string; status: string; type: string; priority: number;
  assignee: string; description: string; created_at: string; updated_at: string; closed_at: string | null;
  session_id?: string;
  activity?: "driven" | "idle" | "stalled";
  ask?: string;
  links?: { ref: string; kind?: string }[];
};

async function mount(node: ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  await act(async () => root!.render(h(QueryClientProvider, { client }, h(ToastProvider, null, node))));
}
async function changeValue(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const prototype = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function flush() {
  await act(async () => {});
}
function allText(): string {
  return document.body.textContent ?? "";
}
function button(text: string): HTMLButtonElement {
  const found = Array.from(document.querySelectorAll("button")).find((b) => b.textContent?.trim() === text);
  if (!found) {
    const have = Array.from(document.querySelectorAll("button")).map((b) => b.textContent?.trim()).join(", ");
    throw new Error(`no button "${text}" (have: ${have})`);
  }
  return found as HTMLButtonElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  // The conversations store is a module singleton that TaskDetail now reads for "Started in" —
  // reset it so each case drives its own rows, not a leftover from the test before.
  resetConversationsStore();
  root = null;
  host = null;
});
afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null;
  host = null;
});

describe("Schedules section (§2.8)", () => {
  it("rows show the prompt's first line and plain-words timing — never the job id or cron (r1)", async () => {
    await mount(h(SchedulesSection, ctx({ sources: sources({ schedules: src([scheduleItem()]) }) })));
    expect(host!.querySelectorAll("ul.work-group > li > button.work-row")).toHaveLength(1);
    const row = host!.querySelector(".work-row")!;
    expect(row.querySelector(".work-row__title")?.textContent).toBe("Post the weekly standup");
    expect(row.textContent).toContain("Weekdays at 09:00");
    expect(allText()).not.toContain("job-7");
    expect(allText()).not.toContain("0 9 * * 1-5");
  });

  it("detail says When in plain words and keeps the cron only in Technical details (r1)", async () => {
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem()]) }),
      state: { itemId: "schedule:job-7" },
    })));
    expect(allText()).toContain("Weekdays at 09:00");
    expect(allText()).not.toContain("0 9 * * 1-5"); // Technical details starts collapsed
    expect(allText()).not.toContain("job-7");

    await act(async () => button("Technical details").click());
    expect(allText()).toContain("0 9 * * 1-5");
    expect(allText()).toContain("job-7");
  });

  it("When names the job's own zone — its timezone, or UTC when it has none (one clock, §2.3)", async () => {
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem({ timezone: "Asia/Tokyo" })]) }),
      state: { itemId: "schedule:job-7" },
    })));
    expect(allText()).toContain("Weekdays at 09:00 (Asia/Tokyo)");
    await act(async () => root?.unmount());
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem({ timezone: null })]) }),
      state: { itemId: "schedule:job-7" },
    })));
    expect(allText()).toContain("Weekdays at 09:00 (UTC)");
  });

  it.each([null, "Asia/Tokyo"])("Edit preserves stored timezone %s on a prompt-only save", async (timezone) => {
    // Edit is the S8d shared step-2 form (WorkEditDialog): "Then" carries the prompt, the
    // Repeat select shows the human choice ("0 9 * * 1-5" → Weekdays), and a save PUTs the
    // SAME job id with the equivalent schedule — no raw cron anywhere in the form.
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem({ timezone })]) }),
      state: { itemId: "schedule:job-7" },
    })));
    await act(async () => button("Edit").click());
    expect(document.querySelector(".pl-dialog__title")?.textContent).toBe("Edit follow-up");
    const prompt = document.querySelector<HTMLTextAreaElement>(".work-create textarea")!;
    expect(prompt.value).toBe("Post the weekly standup\nand @-mention the team");
    expect(document.querySelector<HTMLSelectElement>(".work-create select")!.value).toBe("weekdays");
    expect(allText()).not.toContain("Job id (optional)");
    expect(document.querySelector(".work-create")!.textContent).not.toContain("0 9 * * 1-5");
    await changeValue(prompt, "Updated prompt");
    expect(button("Save changes").disabled).toBe(false);
    await act(async () => button("Save changes").click());
    expect(apiMock.updateSchedule).toHaveBeenCalledWith("job-7", {
      prompt: "Updated prompt", schedule: "0 9 * * 1-5", timezone,
    });
  });

  it("allows prompt edits to a stored past one-off and preserves its exact timestamp", async () => {
    const schedule = "2020-01-01T09:00:00Z";
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem({ schedule })]) }),
      state: { itemId: "schedule:job-7" },
    })));
    await act(async () => button("Edit").click());
    await changeValue(document.querySelector<HTMLTextAreaElement>(".work-create textarea")!, "Updated prompt");
    expect(button("Save changes").disabled).toBe(false);
    await act(async () => button("Save changes").click());
    expect(apiMock.updateSchedule).toHaveBeenCalledWith("job-7", {
      prompt: "Updated prompt", schedule, timezone: null,
    });
  });

  it("a prompt-only edit of a naive one-shot sends the STORED string back — no viewer-clock re-read, seconds kept", async () => {
    // The scheduler holds a naive ISO as UTC; a rebuild would re-read it as browser-local
    // (shifting it by the viewer's UTC offset) and drop the :45. Untouched schedules go
    // back verbatim — only a schedule the operator actually changed is rebuilt.
    const schedule = "2099-01-05T09:30:45";
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem({ schedule })]) }),
      state: { itemId: "schedule:job-7" },
    })));
    await act(async () => button("Edit").click());
    await changeValue(document.querySelector<HTMLTextAreaElement>(".work-create textarea")!, "Updated prompt");
    await act(async () => button("Save changes").click());
    expect(apiMock.updateSchedule).toHaveBeenCalledWith("job-7", {
      prompt: "Updated prompt", schedule, timezone: null,
    });

    // …but a First run the operator actually touched IS rebuilt from the draft.
    await act(async () => button("Edit").click());
    await changeValue(document.querySelector<HTMLInputElement>(".work-create input[type=datetime-local]")!, "2099-02-01T10:00");
    await act(async () => button("Save changes").click());
    expect(apiMock.updateSchedule).toHaveBeenLastCalledWith("job-7", expect.objectContaining({
      schedule: buildOnce("2099-02-01T10:00"), timezone: null,
    }));
  });

  it("Pause calls the enabled route with false (r2)", async () => {
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem()]) }),
      state: { itemId: "schedule:job-7" },
    })));
    await act(async () => button("Pause").click());
    await flush();
    expect(apiMock.setScheduleEnabled).toHaveBeenCalledWith("job-7", false);
  });

  it("Resume on a paused schedule calls the enabled route with true (r2)", async () => {
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem({ enabled: false, next_fire: null })]) }),
      state: { itemId: "schedule:job-7" },
    })));
    expect(allText()).toContain("paused");
    await act(async () => button("Resume").click());
    await flush();
    expect(apiMock.setScheduleEnabled).toHaveBeenCalledWith("job-7", true);
  });

  it("Delete returns an undo token; Undo redeems it via the undo API (r4)", async () => {
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem()]) }),
      state: { itemId: "schedule:job-7" },
    })));
    await act(async () => button("Delete…").click());
    await act(async () => button("Delete").click());
    await flush();
    expect(apiMock.cancelSchedule).toHaveBeenCalledWith("job-7");
    expect(allText()).toContain("Schedule deleted.");

    await act(async () => button("Undo").click());
    await flush();
    expect(apiMock.undo).toHaveBeenCalledWith("tok-sched");
  });
});

describe("Job health and Recent runs (work-view-additions §2)", () => {
  const field = (label: string) => Array.from(host!.querySelectorAll(".work-detail__field"))
    .find((el) => el.querySelector("dt")?.textContent === label)?.querySelector("dd");
  const settle = async () => {
    for (let i = 0; i < 3; i++) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  };
  const detail = (item: WorkItem) => mount(h(SchedulesSection, ctx({
    sources: sources({ schedules: src([item]) }), state: { itemId: `schedule:${item.id}` },
  })));

  it("a failing job's row meta ends with the failure note; one failure reads 'the last run failed'", () => {
    const base = { schedule: "0 9 * * *", timezone: null };
    expect(scheduleMeta({ ...base, consecutive_failures: 3 })).toBe("Every day at 09:00 · failed the last 3 runs");
    expect(scheduleMeta({ ...base, consecutive_failures: 1 })).toBe("Every day at 09:00 · the last run failed");
    expect(scheduleMeta({ ...base, consecutive_failures: 0 })).toBe("Every day at 09:00");
    expect(scheduleMeta(base)).toBe("Every day at 09:00");
    // A one-shot is an instant: it reads in the job's clock (UTC without a timezone), zone named.
    expect(scheduleMeta({ schedule: "2026-11-21T14:30:00Z", timezone: null }, Date.parse("2026-11-20T10:00:00Z")))
      .toBe("Once, Nov 21 14:30 UTC");
  });

  it("a failing job's section row shows a warning dot and the failure note", async () => {
    await mount(h(SchedulesSection, ctx({ sources: sources({ schedules: src([
      scheduleItem({ consecutive_failures: 3 }), scheduleItem({ id: "job-8" }),
    ]) }) })));
    const [failing, healthy] = Array.from(host!.querySelectorAll(".work-row"));
    expect(failing.querySelector(".pl-dot--warning")).not.toBeNull();
    expect(failing.querySelector(".work-row__meta")?.textContent).toBe("Weekdays at 09:00 · failed the last 3 runs");
    expect(healthy.querySelector(".pl-dot--warning")).toBeNull();
    expect(healthy.querySelector(".work-row__meta")?.textContent).toBe("Weekdays at 09:00");
  });

  it("in Coming up a failing job swaps its clock for a warning dot and keeps the Time badge", async () => {
    const raw = { ...(scheduleItem({ consecutive_failures: 1 }).raw as ScheduledJobRaw) };
    const failing = { ...scheduleItem({ consecutive_failures: 1 }), meta: scheduleMeta(raw) };
    const paused = { ...scheduleItem({ id: "job-9", enabled: false, consecutive_failures: 2 }) };
    const [group] = workGroups([failing, scheduleItem({ id: "job-8" }), paused]).filter((g) => g.id === "coming-up");
    await mount(h(WorkGroupRows, { group, sources: sources({}), onOpen: vi.fn() }));
    const [first, second, third] = Array.from(host!.querySelectorAll(".work-row"));
    expect(first.querySelector(".pl-dot--warning")).not.toBeNull();
    expect(first.querySelector(".work-row__meta")?.textContent).toBe("Weekdays at 09:00 · the last run failed");
    expect(first.querySelector(".pl-badge")?.textContent).toBe("Time");
    expect(first.querySelector(".pl-badge")?.className).not.toContain("warning");
    expect(second.querySelector(".pl-dot--warning")).toBeNull();
    // A paused job keeps its pause glyph: it must never pass for an active one.
    expect(third.querySelector(".pl-dot--warning")).toBeNull();
  });

  it("Health says how many runs failed, when it last worked (job clock) and the last error; full error in Technical details", async () => {
    await detail(scheduleItem({
      timezone: "Europe/Berlin", consecutive_failures: 3,
      last_ok: "2020-11-16T13:00:00+00:00", last_error: "Gateway timed out after 30 s",
    }));
    await settle();
    expect(field("Health")?.firstChild?.textContent).toBe("Failed the last 3 runs. Last worked Nov 16 2020 14:00 Europe/Berlin.");
    expect(field("Health")?.querySelector(".job-health__error")?.textContent).toBe("Gateway timed out after 30 s");
    expect(Array.from(host!.querySelectorAll(".work-detail__tech-row dt")).map((dt) => dt.textContent)).not.toContain("Last error");
    const summaries = Array.from(document.querySelectorAll("button")).filter((b) => b.textContent?.trim() === "Technical details");
    await act(async () => summaries[summaries.length - 1].click());
    expect(Array.from(host!.querySelectorAll(".work-detail__tech-row dt")).map((dt) => dt.textContent)).toContain("Last error");
  });

  it("Health reads 'Last worked …' on a healthy job, 'Hasn't run yet.' before a first run, and one failure in words", async () => {
    await detail(scheduleItem({ last_ok: "2020-11-16T14:00:00Z" }));
    expect(field("Health")?.textContent).toBe("Last worked Nov 16 2020 14:00 UTC.");
    await act(async () => root?.unmount());
    await detail(scheduleItem({ last_fire: null }));
    expect(field("Health")?.textContent).toBe("Hasn't run yet.");
    await act(async () => root?.unmount());
    await detail(scheduleItem({ consecutive_failures: 1 }));
    expect(field("Health")?.textContent).toBe("The last run failed.");
  });

  it("Next run and Last run read in the job's clock with the zone name", async () => {
    await detail(scheduleItem({ timezone: "Europe/Berlin", last_fire: "2020-11-07T09:00:00Z" }));
    expect(field("Last run")?.textContent).toBe("Ran Nov 7 2020 10:00 Europe/Berlin");
    expect(field("Next run")?.textContent).toMatch(/\(Jan 1 2099 10:00 Europe\/Berlin\)$/);
  });

  it("a scheduled job's detail mounts Recent runs between the actions and Technical details, asking for 20", async () => {
    apiMock.scheduleRuns.mockResolvedValueOnce({ job_id: "job-7", runs: [
      { scheduled_for: "2020-11-07T09:00:00+00:00", state: "settled", task_state: "failed", ok: false, error: "Gateway timed out" },
    ] });
    await detail(scheduleItem());
    await settle();
    expect(apiMock.scheduleRuns).toHaveBeenCalledWith("job-7", 20);
    const runs = host!.querySelector("section.job-runs")!;
    expect(runs.querySelector("h3")?.textContent).toBe("Recent runs");
    expect(runs.querySelector("li .job-run__word")?.textContent).toBe("Failed");
    expect(runs.querySelector("li .pl-chip-meta")?.textContent).toBe("Nov 7 2020 09:00 UTC");
    const order = (a: Element, b: Element) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(order(host!.querySelector(".work-detail__actions")!, runs)).toBeTruthy();
    const technical = Array.from(host!.querySelectorAll(".work-detail > .technical-details")).pop()!;
    expect(order(runs, technical)).toBeTruthy();
  });

  it("a wait rides the schedule detail but gets no Recent runs", async () => {
    await detail(scheduleItem({ id: "wait:abc" }));
    await settle();
    expect(host!.querySelector(".work-detail")).not.toBeNull();
    expect(host!.querySelector(".job-runs")).toBeNull();
    expect(allText()).not.toContain("Recent runs");
    expect(apiMock.scheduleRuns).not.toHaveBeenCalled();
  });
});

describe("Tasks section (§2.7)", () => {
  it("detail shows the §2.7 fields with type and the priority word (r3)", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem()]) }),
      state: { itemId: "task:task-3" },
    })));
    expect(allText()).toContain("Fix the flaky login test");
    expect(allText()).toContain("Bug · high"); // priority 1 → high
    expect(allText()).toContain("Assigned to casey");
    expect(allText()).toContain("Updated");
    expect(allText()).toContain("Steps to reproduce are in the ticket.");
    expect(allText()).not.toContain("task-3"); // id only in Technical details
  });

  it("Edit seeds the task dialog and saves through the update API", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem()]) }),
      state: { itemId: "task:task-3" },
    })));
    await act(async () => button("Edit").click());
    const title = document.querySelector<HTMLInputElement>('[data-testid="task-create-title"]')!;
    expect(title.value).toBe("Fix the flaky login test");
    await changeValue(title, "Fix login");
    await act(async () => button("Save changes").click());
    expect(apiMock.updateTask).toHaveBeenCalledWith("task-3", {
      title: "Fix login", description: "Steps to reproduce are in the ticket.", type: "bug", priority: 1,
    });
  });

  it("maps numeric priority 0–3 to urgent / high / normal / low (r3)", async () => {
    const items = [0, 1, 2, 3].map((priority) =>
      taskItem({ id: `t${priority}`, title: `t${priority}`, priority }),
    );
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src(items) }) })));
    const metas = Array.from(host!.querySelectorAll(".work-row__meta")).map((n) => n.textContent);
    expect(host!.querySelectorAll("ul.work-group > li > button.work-row")).toHaveLength(4);
    expect(metas).toEqual(["Bug · urgent", "Bug · high", "Bug · normal", "Bug · low"]);
  });

  it("an in-progress task offers Mark open (→ open) and Close (the close route)", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "in_progress" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await act(async () => button("Mark open").click());
    await flush();
    expect(apiMock.updateTask).toHaveBeenCalledWith("task-3", { status: "open" });

    await act(async () => button("Close").click());
    await flush();
    expect(apiMock.closeTask).toHaveBeenCalledWith("task-3");
  });

  it("a not-started job offers Start now (→ in_progress) and Close job (the close route) (r1)", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "open" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await act(async () => button("Start now").click());
    await flush();
    expect(apiMock.updateTask).toHaveBeenCalledWith("task-3", { status: "in_progress" });

    await act(async () => button("Close job").click());
    await flush();
    expect(apiMock.closeTask).toHaveBeenCalledWith("task-3");
  });

  it("a failed Start now keeps the action in reach and reports a warning (r1)", async () => {
    apiMock.updateTask.mockRejectedValueOnce(new Error("offline"));
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "open" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await act(async () => button("Start now").click());
    await flush();
    expect(allText()).toContain("Couldn't");
    // the row stays actionable: Start now is still there, alongside a Try again
    expect(() => button("Start now")).not.toThrow();
    expect(() => button("Try again")).not.toThrow();
  });

  it("a closed task offers Reopen (→ open) and no Start (r1)", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "closed", closed_at: "2026-11-09T10:00:00Z" })]) }),
      state: { itemId: "task:task-3" },
    })));
    expect(() => button("Start")).toThrow();
    expect(() => button("Start now")).toThrow();
    await act(async () => button("Reopen").click());
    await flush();
    expect(apiMock.updateTask).toHaveBeenCalledWith("task-3", { status: "open" });
    expect(apiMock.closeTask).not.toHaveBeenCalled();
  });

  it("a stale job's Close asks why, sends the trimmed reason to the close route, and offers Open chat", async () => {
    const openChat = vi.fn();
    const stale = taskItem({ updated_at: new Date(Date.now() - 2 * 86_400_000).toISOString(), session_id: "chat-9" });
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([stale]) }),
      state: { itemId: "task:task-3" },
      decisionActions: { agentOnline: true, openSourceChat: openChat, answerDecision: vi.fn() },
    })));
    expect(allText()).toContain("No activity for 2 days"); // the muted Activity field
    await act(async () => button("Close…").click());
    expect(allText()).toContain("Close this job?");
    // The confirm stays primary — closing a quiet job is housekeeping, not destruction.
    expect(button("Close job").className).not.toContain("pl-btn--danger");
    await changeValue(document.querySelector<HTMLInputElement>(".work-detail__confirm input")!, "  Shipped elsewhere ");
    await act(async () => button("Close job").click());
    await flush();
    expect(apiMock.closeTask).toHaveBeenCalledWith("task-3", "Shipped elsewhere");
    await act(async () => button("Open chat").click());
    expect(openChat).toHaveBeenCalledWith("chat-9");
  });

  it("a fresh in-progress job keeps the plain one-step Close and no Open chat", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ session_id: "chat-9" })]) }),
      state: { itemId: "task:task-3" },
    })));
    expect(allText()).not.toContain("No activity");
    expect(() => button("Open chat")).toThrow();
    await act(async () => button("Close").click());
    await flush();
    expect(apiMock.closeTask).toHaveBeenCalledWith("task-3");
  });

  it("Delete returns an undo token; Undo redeems it via the undo API (r4)", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem()]) }),
      state: { itemId: "task:task-3" },
    })));
    await act(async () => button("Delete…").click());
    await act(async () => button("Delete").click());
    await flush();
    expect(apiMock.deleteTask).toHaveBeenCalledWith("task-3");
    expect(allText()).toContain("Job deleted.");

    await act(async () => button("Undo").click());
    await flush();
    expect(apiMock.undo).toHaveBeenCalledWith("tok-task");
  });
});

describe("Jobs in the list (§2, r1)", () => {
  const badges = () => Array.from(host!.querySelectorAll(".pl-badge")).map((b) => b.textContent);

  it("a not-started job reads as Job with a dashed icon and 'not started · added <when>'", async () => {
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([taskItem({ status: "open" })]) }) })));
    const row = host!.querySelector(".work-row")!;
    expect(row.querySelector(".pl-badge")?.textContent).toBe("Job");
    expect(row.querySelector(".work-row__meta")?.textContent).toMatch(/^not started · added /);
    expect(row.querySelector(".work-row__icon")).not.toBeNull(); // the dashed circle
    // Tasks fold into jobs — no Task tag anywhere.
    expect(badges()).not.toContain("Task");
  });

  it("a blocked job stays in Doing now and says blocked, never not-started", async () => {
    // The production shape: the task API carries no start history, so a bare blocked row
    // (nothing sets `started`) counts as started work — blocked means someone acted on it.
    const blocked = taskItem({ status: "blocked" });
    expect(groupFor(blocked as GroupableWorkItem)).toBe("doing-now");
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([blocked]) }) })));
    expect(host!.querySelector(".work-row__meta")?.textContent).toBe("blocked");
    expect(host!.querySelector(".work-row__icon")).toBeNull(); // no dashed circle — it has started
    expect(allText()).not.toContain("not started");
  });

  it("an adapter that KNOWS a blocked job never started can demote it (started: false)", async () => {
    const blocked = { ...taskItem({ status: "blocked" }), started: false };
    expect(groupFor(blocked as GroupableWorkItem)).toBe("coming-up");
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([blocked]) }) })));
    expect(host!.querySelector(".work-row__meta")?.textContent).toContain("not started");
  });

  it("a day-quiet in_progress job leads its meta with the muted stale line; a fresh one shows none", async () => {
    const stale = taskItem({ updated_at: new Date(Date.now() - 3 * 86_400_000).toISOString() });
    const fresh = taskItem({ id: "t-fresh", title: "t-fresh" });
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([stale, fresh]) }) })));
    const metas = Array.from(host!.querySelectorAll(".work-row__meta")).map((n) => n.textContent);
    expect(metas).toEqual(["No activity for 3 days · Bug · high", "Bug · high"]);
    // No warning paint anywhere — the stale row keeps the neutral Job badge (operator copy).
    expect(host!.querySelector(".pl-badge--warning")).toBeNull();
  });

  it("an undriven in_progress job reads 'in progress · idle Nm', switching to hours at 60 min (S12b)", async () => {
    const idle45 = taskItem({ id: "idle45", title: "idle45", activity: "idle", updated_at: new Date(Date.now() - 45 * 60_000).toISOString() });
    const idle90 = taskItem({ id: "idle90", title: "idle90", activity: "idle", updated_at: new Date(Date.now() - 90 * 60_000).toISOString() });
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([idle45, idle90]) }) })));
    const metas = Array.from(host!.querySelectorAll(".work-row__meta")).map((n) => n.textContent);
    expect(metas).toEqual(["in progress · idle 45m · Bug · high", "in progress · idle 1h · Bug · high"]);
    // Muted copy, never a warning badge (operator-copy rule).
    expect(host!.querySelector(".pl-badge--warning")).toBeNull();
  });

  it("an idle in_progress job with no usable timestamp still reads 'in progress · idle', no guessed span (S12b)", async () => {
    // Defensive: bd-2grd8 always stamps updated_at on in_progress tasks, but if a row arrives
    // without one, lead with the idle flag we DO have rather than inventing a duration.
    const noStamp = taskItem({ id: "nostamp", title: "nostamp", activity: "idle", updated_at: "", created_at: "" });
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([noStamp]) }) })));
    expect(host!.querySelector(".work-row__meta")?.textContent).toBe("in progress · idle · Bug · high");
    expect(host!.querySelector(".pl-badge--warning")).toBeNull();
  });

  it("a stalled in_progress job leads with 'stalled', not its type (S12b)", async () => {
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([taskItem({ activity: "stalled" })]) }) })));
    expect(host!.querySelector(".work-row__meta")?.textContent).toBe("stalled · Bug · high");
    expect(host!.querySelector(".pl-badge--warning")).toBeNull();
  });

  it("a driven or field-less in_progress job keeps its plain type · priority, never 'idle' (S12b)", async () => {
    const driven = taskItem({ id: "driven", title: "driven", activity: "driven" });
    const legacy = taskItem({ id: "legacy", title: "legacy" }); // older server: no activity field
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([driven, legacy]) }) })));
    const metas = Array.from(host!.querySelectorAll(".work-row__meta")).map((n) => n.textContent);
    expect(metas).toEqual(["Bug · high", "Bug · high"]);
    expect(allText()).not.toContain("idle");
  });

  it("the 24h stale line still wins over an idle flag (S12b)", async () => {
    const stale = taskItem({ activity: "idle", updated_at: new Date(Date.now() - 2 * 86_400_000).toISOString() });
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([stale]) }) })));
    expect(host!.querySelector(".work-row__meta")?.textContent).toBe("No activity for 2 days · Bug · high");
  });

  it("a closed job leaves the list — finished work leaves Work", async () => {
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([taskItem({ status: "closed", closed_at: "2026-11-09T10:00:00Z" })]) }) })));
    expect(host!.querySelectorAll("ul.work-group > li").length).toBe(0);
    expect(allText()).toContain("No jobs");
  });
});

// Shared by the waiting-provenance cases and the open-job "Started in" cases below.
function startedConvRow(over: Partial<ConversationRow> = {}): ConversationRow {
  return {
    session_id: "ctx-55",
    title: "Friction triage",
    origin_label: "From Claude Code for Josh",
    group: "for_you",
    last_updated: "2026-11-14T09:00:00Z",
    turn_count: 3,
    active: false,
    last_state: "input-required",
    ...over,
  };
}
// The conversations list (and, for a waiting job, its source check) resolve asynchronously, and
// react-query delivers each result on a timer tick. Wait for the caller's condition itself — by
// default until the "Checking source chat…" placeholder clears — and fail here if it never holds.
// (Counting a fixed number of flushes gave up silently on a slow CI worker, so the assertion that
// followed read the placeholder.)
async function settleUntil(ready: () => boolean = () => !allText().includes("Checking source chat")) {
  await vi.waitFor(async () => {
    await act(async () => { await Promise.resolve(); });
    if (!ready()) throw new Error("the detail did not settle");
  }, { timeout: 5000, interval: 5 });
}
/** The "Started in" (or waiting "Source") field by its dt label, or null when it isn't drawn. */
function labeledField(label: string): HTMLElement | null {
  return Array.from(document.querySelectorAll<HTMLElement>(".work-detail__field"))
    .find((field) => field.querySelector("dt")?.textContent === label) ?? null;
}

describe("Waiting-task source names the conversation (§4.6, criterion 7)", () => {
  function waiting(over: Partial<TaskRaw> = {}): WorkItem {
    return taskItem({ status: "waiting", session_id: "ctx-55", ...over });
  }

  it("a listed A2A thread is its title as a link, with the origin alongside (criterion 7 ≡ r1)", async () => {
    apiMock.listConversations.mockResolvedValueOnce({ conversations: [startedConvRow()] });
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([waiting()]) }),
      state: { itemId: "task:task-3" },
    })));
    await settleUntil(() => !!document.querySelector('a[href="#/conversations/ctx-55"]'));
    const source = labeledField("Source")!;
    const link = source.querySelector<HTMLAnchorElement>('a.pl-link[href="#/conversations/ctx-55"]')!;
    expect(link.textContent).toBe("Friction triage"); // the thread title, not the old caller phrase
    expect(source.querySelector("dd")?.textContent).toBe("Friction triage · From Claude Code for Josh");
    expect(link.closest(".pl-tip-wrap")).not.toBeNull(); // the full title lives in a Tooltip
    expect(link.style.textOverflow).toBe("ellipsis"); // cut to one line
    await act(async () => link.click());
    expect(navigateMock).toHaveBeenCalledWith({ kind: "conversation", contextId: "ctx-55" });
  });

  it("falls back to an 'Open the conversation' link when the thread isn't listed (r3)", async () => {
    apiMock.listConversations.mockResolvedValueOnce({ conversations: [] });
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([waiting()]) }),
      state: { itemId: "task:task-3" },
    })));
    await settleUntil();
    const link = document.querySelector<HTMLAnchorElement>('a.pl-link[href="#/conversations/ctx-55"]');
    expect(link).not.toBeNull();
    expect(link!.textContent).toBe("Open the conversation");
    expect(allText()).not.toContain("From Claude Code"); // no origin without a listed thread
  });

  it("a job: source keeps its plain 'Started by a background job' text, no link (r5)", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([waiting({ session_id: "job:weekly" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await settleUntil();
    expect(allText()).toContain("Started by a background job");
    expect(document.querySelector('a[href^="#/conversations/"]')).toBeNull();
  });

  it("a deleted chat keeps its plain 'Its chat was deleted' text, no link (r6)", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([waiting({ session_id: "chat-9" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await settleUntil();
    expect(allText()).toContain("Its chat was deleted");
    expect(document.querySelector('a[href^="#/conversations/"]')).toBeNull();
  });
});

describe("'Started in' on an open job (§11.1 items 5–6)", () => {
  it("names the A2A thread right after Type and priority, linking into it (r1)", async () => {
    apiMock.listConversations.mockResolvedValueOnce({ conversations: [startedConvRow()] });
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "in_progress", session_id: "ctx-55" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await settleUntil(() => !!document.querySelector('a[href="#/conversations/ctx-55"]'));
    const labels = Array.from(document.querySelectorAll(".work-detail__field dt")).map((n) => n.textContent);
    expect(labels.slice(0, 2)).toEqual(["Type and priority", "Started in"]);
    const field = labeledField("Started in")!;
    const link = field.querySelector<HTMLAnchorElement>('a.pl-link[href="#/conversations/ctx-55"]')!;
    expect(link.textContent).toBe("Friction triage");
    expect(field.querySelector("dd")?.textContent).toBe("Friction triage · From Claude Code for Josh");
    expect(link.closest(".pl-tip-wrap")).not.toBeNull();
    await act(async () => link.click());
    expect(navigateMock).toHaveBeenCalledWith({ kind: "conversation", contextId: "ctx-55" });
  });

  it("links 'Open the conversation' when the thread isn't in the store (r3)", async () => {
    apiMock.listConversations.mockResolvedValueOnce({ conversations: [] });
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "in_progress", session_id: "ctx-55" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await settleUntil(() => !!document.querySelector('a[href="#/conversations/ctx-55"]'));
    expect(labeledField("Started in")?.querySelector("dd")?.textContent).toBe("Open the conversation");
  });

  it.each([
    ["in_progress", 8, false, false, false],
    ["waiting", 8, false, false, false],
    ["in_progress", 6, false, false, true],
    ["in_progress", 8, true, false, true],
    ["in_progress", 8, false, true, true],
  ])("keeps source links only when usable (%s, %s days, listed=%s, failed=%s)", async (status, days, listed, failed, linked) => {
    if (failed) apiMock.listConversations.mockRejectedValueOnce(new Error("offline"));
    else apiMock.listConversations.mockResolvedValueOnce({ conversations: listed ? [startedConvRow()] : [] });
    const timestamp = new Date(Date.now() - days * 86_400_000).toISOString();
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status, session_id: "ctx-55", created_at: timestamp, updated_at: timestamp })]) }),
      state: { itemId: "task:task-3" },
    })));
    await settleUntil(() => ["ready", "error"].includes(conversationsSnapshot().status) && !allText().includes("Checking source chat"));
    const field = labeledField(status === "waiting" ? "Source" : "Started in")!;
    expect(!!field.querySelector('a[href="#/conversations/ctx-55"]')).toBe(linked);
    if (!linked) {
      expect(field.querySelector("dd")?.textContent).toBe("Started in a conversation that's no longer kept");
      expect(field.querySelector("dd span")?.getAttribute("style")).toContain("--pl-color-fg-muted");
    } else {
      expect(field.querySelector("dd")?.textContent).toContain(listed ? "Friction triage" : "Open the conversation");
    }
  });

  it("links a live chat source as 'Started in chat <title>' and opens it (r4)", async () => {
    const chat = chatStore.createSession();
    chatStore.renameSession(chat.id, "Release review");
    const openChat = vi.fn();
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "in_progress", session_id: chat.id })]) }),
      state: { itemId: "task:task-3" },
      decisionActions: { agentOnline: true, openSourceChat: openChat, answerDecision: vi.fn() },
    })));
    await flush();
    const field = labeledField("Started in")!;
    expect(field.querySelector("dd")?.textContent).toBe("Started in chat Release review");
    await act(async () => field.querySelector<HTMLAnchorElement>("a")!.click());
    expect(openChat).toHaveBeenCalledWith(chat.id);
    expect(navigateMock).not.toHaveBeenCalled(); // a chat doesn't route through the conversation link
  });

  it.each(["job:weekly", "background:sweep"])("shows muted 'Started by a background job' for a %s source, no link (r5)", async (source) => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "in_progress", session_id: source })]) }),
      state: { itemId: "task:task-3" },
    })));
    await flush();
    const field = labeledField("Started in")!;
    expect(field.querySelector("dd")?.textContent).toBe("Started by a background job");
    expect(field.querySelector("a")).toBeNull();
  });

  it("draws no 'Started in' field when the source chat is gone from the store (r6)", async () => {
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "in_progress", session_id: "chat-gone" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await flush();
    expect(labeledField("Started in")).toBeNull();
  });

  it("shows no 'Started in' field on a closed job, even with a listed thread (r8)", async () => {
    apiMock.listConversations.mockResolvedValueOnce({ conversations: [startedConvRow()] });
    await mount(h(TasksSection, ctx({
      sources: sources({ tasks: src([taskItem({ status: "closed", closed_at: "2026-11-09T10:00:00Z", session_id: "ctx-55" })]) }),
      state: { itemId: "task:task-3" },
    })));
    await settleUntil(() => conversationsSnapshot().status === "ready"); // let the thread resolve first
    expect(labeledField("Started in")).toBeNull();
    expect(document.querySelector('a[href="#/conversations/ctx-55"]')).toBeNull();
  });
});

describe("A waiting job shows the question and links what it waits on (#5676)", () => {
  const ARTIFACT = "a-1795238555984-27d9a2";
  const ASK = `Approve the scope in ${ARTIFACT} for bd-kms4m.g1 (protoLabsAI/protoAgent#5676)?`;
  const BOARD_KEY = "plugin:project_board:board";
  const railBefore = useUI.getState().railOrder;
  function rail(right: string[]) {
    useUI.setState({ railOrder: { left: ["chat"], right, bottom: [], hidden: [] } });
  }
  function waiting(over: Partial<TaskRaw> = {}): WorkItem {
    return taskItem({
      status: "waiting", session_id: "job:weekly", ask: ASK,
      description: `${ASK}\nAll 5 audits done; 5 repeated problems, 5 house rules.`, ...over,
    });
  }
  async function open(item: WorkItem) {
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([item]) }), state: { itemId: "task:task-3" } })));
    await settleUntil();
  }
  const shownLabels = () => Array.from(document.querySelectorAll(".work-detail__fields:not(.work-detail__fields--technical) dt"))
    .map((node) => node.textContent);
  const technicalLabels = () => Array.from(document.querySelectorAll(".work-detail__fields--technical dt"))
    .map((node) => node.textContent);

  beforeEach(() => { resetPluginViewInbox(); rail([ARTIFACT_VIEW_KEY, BOARD_KEY]); });
  afterEach(() => useUI.setState({ railOrder: railBefore }));

  it("leads with who is asking and the ask, then links and the reply box, stating the ask once (r1)", async () => {
    await open(waiting());
    expect(shownLabels()).toEqual(["Asked by", "Ask", "Links", "Source", "Reply"]);
    expect(labeledField("Asked by")!.querySelector("dd")?.textContent).toBe("Your agent");
    expect(labeledField("Ask")!.querySelector("dd")?.textContent).toBe(ASK);
    expect(allText().split(ASK)).toHaveLength(2); // once on the page before the accordion opens
    await act(async () => button("Technical details").click());
    expect(allText().split(ASK)).toHaveLength(2); // and Description doesn't repeat it
    expect(labeledField("Description")!.querySelector("dd")?.textContent)
      .toBe("All 5 audits done; 5 repeated problems, 5 house rules.");
  });

  it("shows the full description as the fallback ask without repeating it", async () => {
    await open(waiting({ ask: undefined, description: "Can we ship?\nWorking notes." }));
    expect(labeledField("Ask")!.querySelector("dd")?.textContent).toBe("Can we ship?\nWorking notes.");
    await act(async () => button("Technical details").click());
    expect(labeledField("Description")).toBeNull();
    expect(allText().split("Working notes.")).toHaveLength(2);
  });

  it("turns an artifact id, a board card id and a GitHub ref in the ask into links that open the item (r2)", async () => {
    await open(waiting());
    const ask = labeledField("Ask")!;
    const artifact = ask.querySelector<HTMLAnchorElement>('a[data-ask-ref="artifact"]')!;
    expect(artifact.textContent).toBe(ARTIFACT);
    const card = ask.querySelector<HTMLAnchorElement>('a[data-ask-ref="card"]')!;
    expect(card.textContent).toBe("bd-kms4m.g1");
    const cardUrl = new URL(card.href);
    expect(cardUrl.searchParams.get("plugin")).toBe("project_board");
    expect(cardUrl.searchParams.get("vp")).toBe("card=bd-kms4m.g1"); // the board view reads ?card=
    expect(card.target).toBe("_blank"); // a reload would drop a half-written reply
    const issue = ask.querySelector<HTMLAnchorElement>('a[data-ask-ref="github"]')!;
    expect(issue.textContent).toBe("protoLabsAI/protoAgent#5676");
    expect(issue.href).toBe("https://github.com/protoLabsAI/protoAgent/issues/5676");

    // The artifact opens in the Artifact panel, on its newest version, without leaving Work.
    await act(async () => artifact.click());
    const [select] = takePluginViewMessages(ARTIFACT_VIEW_KEY);
    expect(select).toMatchObject({ type: "protoArtifact:select", id: ARTIFACT });
    expect(select.ver as number).toBeGreaterThan(1_000_000);
  });

  it("lists what the ask and the task's links reference once each, under Links (r2)", async () => {
    await open(waiting({ links: [{ ref: "bd-kms4m.g1", kind: "card" }, { ref: "https://example.com/pull/7" }, { ref: "job-2" }] }));
    const items = Array.from(labeledField("Links")!.querySelectorAll("li"));
    expect(items.map((li) => li.textContent)).toEqual([
      "Open the artifact", "Board card bd-kms4m.g1", "protoLabsAI/protoAgent#5676", "https://example.com/pull/7", "job-2",
    ]);
    expect(items.map((li) => !!li.querySelector("a"))).toEqual([true, true, true, true, false]);
  });

  it("leaves a ref as plain text when its panel or board isn't on this agent", async () => {
    rail([]);
    await open(waiting());
    const ask = labeledField("Ask")!;
    expect(ask.querySelector("dd")?.textContent).toBe(ASK);
    expect(Array.from(ask.querySelectorAll("a")).map((a) => a.textContent)).toEqual(["protoLabsAI/protoAgent#5676"]);
    expect(labeledField("Links")!.querySelector("li")?.textContent).toBe(ARTIFACT); // the id, not a dead "Open the artifact"
  });

  it("keeps the record inside Technical details (r3)", async () => {
    await open(waiting());
    for (const label of ["Description", "Type and priority", "Updated", "Created", "Waiting since"]) {
      expect(shownLabels()).not.toContain(label);
    }
    expect(allText()).not.toContain("Bug · high");
    expect(technicalLabels()).toEqual([]); // collapsed: not on the page at all
    await act(async () => button("Technical details").click());
    expect(technicalLabels()).toEqual(["Waiting since", "Description", "Type and priority", "Assignee", "Updated", "Created"]);
    expect(allText()).toContain("Bug · high");
    expect(allText()).toContain("task-3"); // the id stays there too
  });

  it("an open (not waiting) job renders as before: its fields up front, none in Technical details (r3)", async () => {
    await open(taskItem({ status: "open", ask: ASK }));
    expect(shownLabels()).toEqual(["Type and priority", "Assignee", "Updated", "Description", "Created"]);
    expect(document.querySelector("[data-ask-ref]")).toBeNull();
    await act(async () => button("Technical details").click());
    expect(technicalLabels()).toEqual([]);
    expect(allText()).toContain("task-3");
  });

  it("parseAskRefs keeps sentence punctuation out of a ref and ids inside a URL whole", () => {
    expect(parseAskRefs("See bd-c8r5h. Then https://example.com/a-1795238555984-27d9a2/x, ok?")).toEqual([
      "See ", { kind: "card", ref: "bd-c8r5h" }, ". Then ",
      { kind: "url", ref: "https://example.com/a-1795238555984-27d9a2/x" }, ", ok?",
    ]);
    expect(parseAskRefs("data-a-12 and abd-x1 stay text")).toEqual(["data-a-12 and abd-x1 stay text"]);
  });
});

describe("Replying to a waiting job from a From others conversation is private (#5400)", () => {
  const TURN = { task_id: "turn-1", state: "completed", last_updated: null, text: "Ready" };
  function waiting(sessionId: string): WorkItem {
    return taskItem({ status: "waiting", session_id: sessionId, ask: "Can we ship?" });
  }
  async function open(sessionId: string, { peer, turns }: { peer: boolean; turns: boolean }) {
    apiMock.listConversations.mockResolvedValueOnce({ conversations: peer ? [startedConvRow({ session_id: sessionId })] : [] });
    apiMock.chatSessionTurns.mockImplementation(async () => ({ turns: turns ? [TURN] : [] }));
    await mount(h(TasksSection, ctx({ sources: sources({ tasks: src([waiting(sessionId)]) }), state: { itemId: "task:task-3" } })));
    await settleUntil(() => conversationsSnapshot().status === "ready" && !allText().includes("Checking source chat"));
  }
  const replyInput = () => labeledField("Reply")!.querySelector<HTMLInputElement>("input")!;
  async function reply(text: string) {
    await changeValue(replyInput(), text);
    await act(async () => button("Send reply").click());
    await settleUntil(() => apiMock.steerChat.mock.calls.length > 0);
    await flush();
  }
  const controls = () => ({
    buttons: Array.from(document.querySelectorAll(".work-detail button")).map((b) => b.textContent?.trim()),
    fields: Array.from(document.querySelectorAll(".work-detail__fields:not(.work-detail__fields--technical) dt")).map((d) => d.textContent),
  });

  afterEach(() => {
    apiMock.chatSessionTurns.mockImplementation(async () => ({ turns: [] }));
    apiMock.steerChat.mockImplementation(async () => ({ ok: true, id: "reply", pending: 0, started_turn: "turn-2" }));
  });

  it("steers a From others source that has turns with the private flag (r1)", async () => {
    await open("ctx-55", { peer: true, turns: true });
    await reply("Yes, ship it.");
    expect(apiMock.registerSession).not.toHaveBeenCalled();
    const call = apiMock.steerChat.mock.calls[0];
    expect(call[0]).toBe("ctx-55");
    expect(call[2]).toBe("Yes, ship it.");
    expect(call[6]).toBe(true); // isPrivate → `private: true` in the steer body
    expect(call[3]).toBeUndefined(); // text only: no images
    expect(call[4]).toBeUndefined(); // and no incognito
    expect(apiMock.updateTask).toHaveBeenCalledWith("task-3", { session_id: "ctx-55", status: "open" });
  });

  it("a fresh chat- session made for the reply sends no private flag (r2)", async () => {
    await open("ctx-55", { peer: true, turns: false });
    await reply("Yes, ship it.");
    const call = apiMock.steerChat.mock.calls[0];
    expect(call[0]).toMatch(/^chat-/);
    expect(call[0]).not.toBe("ctx-55");
    expect(apiMock.registerSession).toHaveBeenCalledWith(call[0], { title: "Re: Fix the flaky login test" });
    expect(call).toHaveLength(3); // exactly (session, id, text) — no `private` key
  });

  it("an ordinary own chat with turns is steered as before, without the private flag", async () => {
    await open("chat-own", { peer: false, turns: true });
    await reply("Go ahead");
    expect(apiMock.steerChat.mock.calls[0]).toEqual(["chat-own", expect.any(String), "Go ahead"]);
  });

  it("a 409 refusal reads as the plain sentence and keeps the reply in the field (r3)", async () => {
    apiMock.steerChat.mockRejectedValueOnce(new ApiError(409, "a private steer can't answer the peer's own question"));
    await open("ctx-55", { peer: true, turns: true });
    await reply("Yes, ship it.");
    await settleUntil(() => allText().includes(PRIVATE_STEER_WAITING_ON_PEER));
    const shown = labeledField("Reply")!.textContent ?? "";
    expect(shown).not.toContain("409");
    expect(shown).not.toContain("can't answer the peer");
    expect(shown).not.toContain("Reply has not started yet");
    expect(shown).not.toContain("Couldn't finish sending your reply");
    expect(replyInput().value).toBe("Yes, ship it.");
    expect(replyInput().disabled).toBe(false); // never delivered: editable, and a resend starts over
    expect(apiMock.updateTask).not.toHaveBeenCalled();
  });

  it("draws the same controls for a From others source as for an ordinary chat (r4)", async () => {
    await open("ctx-55", { peer: true, turns: true });
    const peer = controls();
    await act(async () => root?.unmount());
    host?.remove();
    resetConversationsStore();
    await open("chat-own", { peer: false, turns: true });
    expect(controls()).toEqual(peer);
    expect(peer.buttons).toContain("Send reply");
    expect(peer.buttons.some((label) => /private/i.test(label ?? ""))).toBe(false); // no toggle
  });
});
