// useWorkItems (#4955 §7.2) — the one data hook over all eight Work sources. Driven through
// the same minimal createRoot/act renderHook the rest of the console uses (no testing-library
// dep), a real QueryClient whose server lists come from spied `api`/`decisionsApi` calls, and
// a faked `onTopic` bus. Pins: every source goes READY with items mapped through ./tone;
// `thisChatOnly` filters only the sources that carry a session id; one push refetches one
// source; a first-load failure is `error` while a post-load failure is `stale` (items kept,
// others untouched); schedules poll until the first `scheduler.changed`; undo + setScheduleEnabled
// hit the new routes.
import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listeners: new Map<string, Set<(data: Record<string, unknown>) => void>>(),
}));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    const set = mocks.listeners.get(topic) ?? new Set();
    set.add(fn);
    mocks.listeners.set(topic, set);
    return () => set.delete(fn);
  },
}));

import { api } from "../lib/api";
import { decisionsApi, type DecisionRow } from "../lib/api/decisions";
import type { DirectiveRow } from "../lib/api/chat";
import type { ActivityEntry, ActivityHistory } from "../lib/types";
import { SCHEDULE_POLL_MS, useWorkItems, type UseWorkItemsParams, type WorkItems, type WorkSourceKind } from "./useWorkItems";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// ── row factories (only the verbose shapes need one) ──────────────────────────
const dec = (id: string, state: string, session: string): DecisionRow => ({
  id, session_id: session, agent: "leadEngineer", question: `decide ${id}`, options: [],
  created_at: "2026-11-09T00:00:00+00:00", state: state as DecisionRow["state"], answer: null,
  answered_at: null, expires_at: null, default_option: null, thread_id: "", interrupt_id: "",
  context: "", default_action: null, withdraw_reason: null, answered_by: null,
  answered_surface: null, default_action_text: "", answer_label: null,
});
const dir = (id: string, status: string, session: string): DirectiveRow => ({
  id, session_id: session, quote: `directive ${id}`, conditions: "", scope: "chat",
  status: status as DirectiveRow["status"], created_at: "2026-11-09T00:00:00+00:00",
  closed_by: null, closed_at: null, close_reason: null,
});
const actEntry = (id: number, state: string): ActivityEntry => ({
  id, created_at: "2026-11-09T00:00:00+00:00", origin: "scheduler", trigger: "job-1",
  priority: "", state, text: `activity ${id}`, task_id: `task-${id}`,
});
const history = (contextId: string, entries: ActivityEntry[]): ActivityHistory => ({
  context_id: contextId, entries, messages: [],
});

type Spies = {
  decisions: ReturnType<typeof vi.spyOn>;
  goals: ReturnType<typeof vi.spyOn>;
  watches: ReturnType<typeof vi.spyOn>;
  tasks: ReturnType<typeof vi.spyOn>;
  schedules: ReturnType<typeof vi.spyOn>;
  background: ReturnType<typeof vi.spyOn>;
  activity: ReturnType<typeof vi.spyOn>;
  directives: ReturnType<typeof vi.spyOn>;
};
let spies: Spies;
let roots: Root[] = [];

function stubEverything(): Spies {
  return {
    decisions: vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue({ decisions: [dec("d1", "pending", "s1")], pending: 1 }),
    goals: vi.spyOn(api, "goals").mockResolvedValue({ goals: [{ session_id: "s1", condition: "ship it", status: "active" }], enabled: true }),
    watches: vi.spyOn(api, "watches").mockResolvedValue({ watches: [{ id: "w1", condition: "watch it", status: "active" }], enabled: true }),
    tasks: vi.spyOn(api, "tasks").mockResolvedValue({ issues: [{ id: "t1", title: "do it", status: "in_progress" }] }),
    schedules: vi.spyOn(api, "schedules").mockResolvedValue({ jobs: [{ id: "j1", prompt: "run", schedule: "0 9 * * *", enabled: true, next_fire: "2026-11-10T09:00:00Z" }], backend: "apscheduler" }),
    background: vi.spyOn(api, "background").mockResolvedValue({ enabled: true, jobs: [{ id: "b1", status: "running", subagent_type: "coder", description: "build", origin_session: "s1" }] }),
    activity: vi.spyOn(api, "activity").mockResolvedValue(history("s1", [actEntry(1, "completed")])),
    directives: vi.spyOn(api, "listDirectives").mockResolvedValue({ directives: [dir("x1", "in_force", "s1")], in_force: 1 }),
  };
}

function mount(params: UseWorkItemsParams) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  roots.push(root);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
  const result = { current: undefined as unknown as WorkItems };
  function Probe(p: UseWorkItemsParams) {
    result.current = useWorkItems(p);
    return null;
  }
  act(() => root.render(h(QueryClientProvider, { client }, h(Probe, params))));
  return result;
}

function emit(topic: string, data: Record<string, unknown> = {}) {
  act(() => mocks.listeners.get(topic)?.forEach((fn) => fn(data)));
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));
  });
}

beforeEach(() => {
  mocks.listeners.clear();
  spies = stubEverything();
});
afterEach(() => {
  roots.forEach((r) => act(() => r.unmount()));
  roots = [];
  focusManager.setFocused(undefined);
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useWorkItems — load, map, filter", () => {
  it("takes every source to ready with items mapped to a WorkStatus", async () => {
    const r = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    const w = r.current;
    (Object.keys(w) as WorkSourceKind[]).forEach((k) => expect(w[k].status).toBe("ready"));
    expect(w.decisions.items[0]).toMatchObject({ kind: "decision", id: "d1", status: "needs-you", sessionId: "s1" });
    expect(w.goals.items[0]).toMatchObject({ kind: "goal", id: "s1", status: "running", sessionId: "s1" });
    expect(w.watches.items[0]).toMatchObject({ kind: "watch", id: "w1", status: "waiting" });
    expect(w.tasks.items[0]).toMatchObject({ kind: "task", id: "t1", status: "running" });
    expect(w.schedules.items[0]).toMatchObject({ kind: "schedule", id: "j1", status: "scheduled" });
    expect(w.background.items[0]).toMatchObject({ kind: "background", id: "b1", status: "running", sessionId: "s1" });
    expect(w.activity.items[0]).toMatchObject({ kind: "activity", id: "1", status: "done" });
    expect(w.directives.items[0]).toMatchObject({ kind: "directive", id: "x1", status: "waiting", sessionId: "s1" });
    expect(w.goals.loadedAt).toEqual(expect.any(Number));
  });

  it("thisChatOnly filters only the sources that carry a session id", async () => {
    spies.decisions.mockResolvedValue({ decisions: [dec("d1", "pending", "s1"), dec("d2", "pending", "s2")], pending: 2 });
    spies.goals.mockResolvedValue({ goals: [{ session_id: "s1", condition: "a", status: "active" }, { session_id: "s2", condition: "b", status: "active" }], enabled: true });
    spies.background.mockResolvedValue({ enabled: true, jobs: [{ id: "b1", status: "running", subagent_type: "c", description: "d", origin_session: "s1" }, { id: "b2", status: "running", subagent_type: "c", description: "e", origin_session: "s2" }] });
    spies.directives.mockResolvedValue({ directives: [dir("x1", "in_force", "s1"), dir("x2", "in_force", "s2")], in_force: 2 });
    spies.activity.mockResolvedValue(history("s2", [actEntry(1, "completed")])); // the activity thread's own session

    const scoped = mount({ sessionId: "s1", thisChatOnly: true });
    await flush();
    const w = scoped.current;
    expect(w.decisions.items.map((i) => i.id)).toEqual(["d1"]);
    expect(w.goals.items.map((i) => i.id)).toEqual(["s1"]);
    expect(w.background.items.map((i) => i.id)).toEqual(["b1"]);
    expect(w.directives.items.map((i) => i.id)).toEqual(["x1"]);
    expect(w.activity.items).toEqual([]); // context s2 ≠ this chat
    // Session-less sources are never filtered out.
    expect(w.watches.items).toHaveLength(1);
    expect(w.tasks.items).toHaveLength(1);
    expect(w.schedules.items).toHaveLength(1);

    const all = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    expect(all.current.decisions.items.map((i) => i.id)).toEqual(["d1", "d2"]);
    expect(all.current.goals.items.map((i) => i.id)).toEqual(["s1", "s2"]);
    expect(all.current.activity.items).toHaveLength(1);
  });
});

describe("useWorkItems — live refresh", () => {
  it("refetches only the source a push names", async () => {
    const r = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    expect(spies.decisions).toHaveBeenCalledTimes(1);
    expect(spies.watches).toHaveBeenCalledTimes(1);
    expect(spies.goals).toHaveBeenCalledTimes(1);
    expect(spies.tasks).toHaveBeenCalledTimes(1);
    expect(spies.background).toHaveBeenCalledTimes(1);
    expect(spies.schedules).toHaveBeenCalledTimes(1);
    expect(spies.directives).toHaveBeenCalledTimes(1);

    emit("watch.changed");
    await flush();
    expect(spies.watches).toHaveBeenCalledTimes(2);
    expect(spies.goals).toHaveBeenCalledTimes(1);
    expect(spies.tasks).toHaveBeenCalledTimes(1);
    expect(spies.background).toHaveBeenCalledTimes(1);

    emit("goal.changed");
    await flush();
    expect(spies.goals).toHaveBeenCalledTimes(2);
    expect(spies.watches).toHaveBeenCalledTimes(2);

    emit("background.completed");
    await flush();
    expect(spies.background).toHaveBeenCalledTimes(2);

    emit("task.changed");
    await flush();
    expect(spies.tasks).toHaveBeenCalledTimes(2);

    // A `decision.changed` refetches decisions (prefix-keyed) and nothing else.
    emit("decision.changed", { id: "d1", session_id: "s1", state: "answered" });
    await flush();
    expect(spies.decisions).toHaveBeenCalledTimes(2);
    expect(spies.directives).toHaveBeenCalledTimes(1);

    // A `directive.changed` refetches directives (its session-scoped key matched by prefix).
    emit("directive.changed", { id: "x1", session_id: "s1" });
    await flush();
    expect(spies.directives).toHaveBeenCalledTimes(2);
    expect(spies.decisions).toHaveBeenCalledTimes(2);

    emit("scheduler.changed", { job_id: "j1", action: "added" });
    await flush();
    expect(spies.schedules).toHaveBeenCalledTimes(2);
    // Nothing else moved on that scheduler push.
    expect(spies.goals).toHaveBeenCalledTimes(2);
    expect(spies.watches).toHaveBeenCalledTimes(2);
    expect(r.current.decisions.status).toBe("ready");
  });

  it("polls schedules every 60s until a scheduler.changed, then stops", async () => {
    vi.useFakeTimers();
    focusManager.setFocused(true);
    mount({ thisChatOnly: false });
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    expect(spies.schedules).toHaveBeenCalledTimes(1);

    await act(async () => void (await vi.advanceTimersByTimeAsync(SCHEDULE_POLL_MS)));
    expect(spies.schedules).toHaveBeenCalledTimes(2);
    await act(async () => void (await vi.advanceTimersByTimeAsync(SCHEDULE_POLL_MS)));
    expect(spies.schedules).toHaveBeenCalledTimes(3);

    emit("scheduler.changed", { job_id: "j1", action: "added" });
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    expect(spies.schedules).toHaveBeenCalledTimes(4); // the push's own invalidation

    await act(async () => void (await vi.advanceTimersByTimeAsync(SCHEDULE_POLL_MS * 3)));
    expect(spies.schedules).toHaveBeenCalledTimes(4); // polling is off
  });
});

describe("useWorkItems — error vs stale", () => {
  it("reports error for a first-load failure and leaves the others ready", async () => {
    spies.goals.mockRejectedValue(new Error("boom"));
    const r = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    const w = r.current;
    expect(w.goals.status).toBe("error");
    expect(w.goals.items).toEqual([]);
    expect(w.goals.loadedAt).toBeNull();
    expect(w.goals.error).toBeInstanceOf(Error);
    expect(w.watches.status).toBe("ready");
    expect(w.tasks.status).toBe("ready");
  });

  it("keeps items and reports stale when a refresh fails after a good load", async () => {
    const r = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    expect(r.current.watches.status).toBe("ready");
    const loadedAt = r.current.watches.loadedAt;

    spies.watches.mockRejectedValue(new Error("offline"));
    emit("watch.changed");
    await flush();
    const w = r.current;
    expect(w.watches.status).toBe("stale");
    expect(w.watches.items).toHaveLength(1); // last good rows kept
    expect(w.watches.loadedAt).toBe(loadedAt); // last good load time preserved
    expect(w.goals.status).toBe("ready"); // one source failing never touches another
  });

  it("retry() refetches a failed source", async () => {
    spies.decisions.mockRejectedValueOnce(new Error("down")).mockResolvedValue({ decisions: [dec("d1", "pending", "s1")], pending: 1 });
    const r = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    expect(r.current.decisions.status).toBe("error");
    r.current.decisions.retry();
    await flush();
    expect(r.current.decisions.status).toBe("ready");
    expect(r.current.decisions.items[0].id).toBe("d1");
  });
});

describe("work API helpers", () => {
  it("undo POSTs the token to /api/undo/{token}", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ restored: true, kind: "task" })));
    vi.stubGlobal("fetch", fetchMock);
    expect(await api.undo("tok 1")).toEqual({ restored: true, kind: "task" });
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/undo/tok%201");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
  });

  it("setScheduleEnabled POSTs {enabled} to the job's enabled route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ job: { id: "j1", prompt: "p", schedule: "0 9 * * *", enabled: false } })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await api.setScheduleEnabled("j:1", false);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/scheduler/jobs/j%3A1/enabled");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ enabled: false });
  });
});
