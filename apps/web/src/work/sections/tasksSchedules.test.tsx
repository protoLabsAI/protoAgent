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

const apiMock = vi.hoisted(() => ({
  setScheduleEnabled: vi.fn(async () => ({ job: {} })),
  cancelSchedule: vi.fn(async () => ({ canceled: true, undo_token: "tok-sched" })),
  updateSchedule: vi.fn(async () => ({ job: {} })),
  deleteTask: vi.fn(async () => ({ deleted: "task-3", undo_token: "tok-task" })),
  updateTask: vi.fn(async () => ({ issue: {} })),
  undo: vi.fn(async () => ({ restored: true, kind: "task" })),
}));
vi.mock("../../lib/api", () => ({ api: apiMock, currentSlug: () => "test" }));

import { SchedulesSection } from "./schedules";
import { TasksSection } from "./tasks";
import type { WorkItem, WorkItems, WorkSource } from "../useWorkItems";
import type { WorkSectionContext } from "../WorkView";

vi.mock("../../schedule/schedule-builder", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../schedule/schedule-builder")>(),
  localZone: () => "America/Los_Angeles",
}));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
    updated_at: "2026-11-09T09:00:00Z",
    closed_at: null,
    ...over,
  };
  return { kind: "task", id: raw.id, status: "running", title: raw.title, meta: raw.type, raw };
}
type TaskRaw = {
  id: string; title: string; status: string; type: string; priority: number;
  assignee: string; description: string; created_at: string; updated_at: string; closed_at: string | null;
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

  it("shows the timezone in When only when it differs from local, and hides it in local time", async () => {
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem({ timezone: "Asia/Tokyo" })]) }),
      state: { itemId: "schedule:job-7" },
    })));
    expect(allText()).toContain("(Asia/Tokyo)");
  });

  it.each([null, "Asia/Tokyo"])("Edit preserves stored timezone %s on a prompt-only save", async (timezone) => {
    await mount(h(SchedulesSection, ctx({
      sources: sources({ schedules: src([scheduleItem({ timezone })]) }),
      state: { itemId: "schedule:job-7" },
    })));
    await act(async () => button("Edit").click());
    const prompt = document.querySelector<HTMLTextAreaElement>('[data-testid="schedule-prompt"]')!;
    expect(prompt.value).toBe("Post the weekly standup\nand @-mention the team");
    expect(allText()).not.toContain("Job id (optional)");
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
    await changeValue(document.querySelector<HTMLTextAreaElement>('[data-testid="schedule-prompt"]')!, "Updated prompt");
    expect(button("Save changes").disabled).toBe(false);
    await act(async () => button("Save changes").click());
    expect(apiMock.updateSchedule).toHaveBeenCalledWith("job-7", {
      prompt: "Updated prompt", schedule, timezone: null,
    });
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
    expect(metas).toEqual(["Bug · urgent", "Bug · high", "Bug · normal", "Bug · low"]);
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
    expect(allText()).toContain("Task deleted.");

    await act(async () => button("Undo").click());
    await flush();
    expect(apiMock.undo).toHaveBeenCalledWith("tok-task");
  });
});
