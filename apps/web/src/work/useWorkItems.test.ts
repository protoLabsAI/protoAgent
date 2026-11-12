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
  conn: new Set<(connected: boolean) => void>(),
}));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    const set = mocks.listeners.get(topic) ?? new Set();
    set.add(fn);
    mocks.listeners.set(topic, set);
    return () => set.delete(fn);
  },
  onConnectionChange: (fn: (connected: boolean) => void) => {
    mocks.conn.add(fn);
    fn(true);
    return () => mocks.conn.delete(fn);
  },
}));

import { directiveStore } from "../chat/useDirectives";
import { workGroups } from "./workGroups";
import { api } from "../lib/api";
import { decisionsApi, type DecisionRow } from "../lib/api/decisions";
import type { DirectiveRow } from "../lib/api/chat";
import type { ActivityEntry, ActivityHistory } from "../lib/types";
import { SCHEDULE_POLL_MS, activityTitle, useWorkItems, type UseWorkItemsParams, type WorkItems, type WorkSourceKind } from "./useWorkItems";

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
  inbox: ReturnType<typeof vi.spyOn>;
  sessions: ReturnType<typeof vi.spyOn>;
  directives: ReturnType<typeof vi.spyOn>;
};
let spies: Spies;
let roots: Root[] = [];

function stubEverything(): Spies {
  return {
    inbox: vi.spyOn(api, "inbox").mockResolvedValue({ items: [] }),
    sessions: vi.spyOn(api, "chatSessions").mockResolvedValue({ sessions: [] }),
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
    expect(w.decisions.items[0].meta).toMatch(/^Asked /); // §2.4 Overview pin: "Asked {time}", not the agent name
    expect(w.goals.items[0]).toMatchObject({ kind: "goal", id: "s1", status: "running", sessionId: "s1" });
    expect(w.watches.items[0]).toMatchObject({ kind: "watch", id: "w1", status: "waiting" });
    expect(w.tasks.items[0]).toMatchObject({ kind: "task", id: "t1", status: "running" });
    expect(w.schedules.items[0]).toMatchObject({ kind: "schedule", id: "j1", status: "scheduled" });
    // Overview rows read in plain words, like their sections: never a raw cron, a bare type
    // or a trigger keyword (work-view spec §4; release acceptance bd-nfd6g.17).
    expect(w.schedules.items[0].meta).toBe("Every day at 09:00");
    expect(w.watches.items[0].meta).toBe("Fires once when met");
    expect(w.tasks.items[0].meta).toBe("Task");
    expect(w.background.items[0]).toMatchObject({ kind: "background", id: "b1", status: "running", sessionId: "s1" });
    expect(w.activity.items).toEqual([]);
    expect(spies.activity).not.toHaveBeenCalled();
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
    expect(w.decisions.items.map((i) => i.id)).toEqual(["d1", "d2"]);
    expect(w.goals.items.map((i) => i.id)).toEqual(["s1"]);
    expect(w.background.items.map((i) => i.id)).toEqual(["b1"]);
    expect(w.directives.items.map((i) => i.id)).toEqual(["x1"]);
    expect(w.activity.items).toEqual([]); // context s2 ≠ this chat
    // Shared scope requires known ownership outside Needs you.
    expect(w.watches.items).toHaveLength(0);
    expect(w.tasks.items).toHaveLength(0);
    expect(w.schedules.items).toHaveLength(0);

    const all = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    expect(all.current.decisions.items.map((i) => i.id)).toEqual(["d1", "d2"]);
    expect(all.current.goals.items.map((i) => i.id)).toEqual(["s1", "s2"]);
    expect(all.current.activity.items).toHaveLength(0);
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

    spies.activity.mockResolvedValue(history("s1", [actEntry(2, "completed"), actEntry(1, "completed")]));
    emit("activity.message", { text: "activity 2", origin: "scheduler" });
    await flush();
    expect(spies.activity).not.toHaveBeenCalled();
    expect(spies.inbox).toHaveBeenCalledTimes(2);
    expect(r.current.activity.items).toEqual([]);
    expect(spies.tasks).toHaveBeenCalledTimes(2);
    expect(spies.background).toHaveBeenCalledTimes(2);

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

  it("refetches decisions once on an offline → online edge, not on subscribe", async () => {
    const r = mount({ thisChatOnly: false });
    await flush();
    expect(spies.decisions).toHaveBeenCalledTimes(1); // the subscribe-time "connected" is not a reconnect

    spies.decisions.mockResolvedValue({ decisions: [dec("d1", "answered", "s1")], pending: 0 });
    for (const fn of mocks.conn) fn(false);
    await flush();
    expect(spies.decisions).toHaveBeenCalledTimes(1);
    for (const fn of mocks.conn) fn(true);
    await flush();
    expect(spies.decisions).toHaveBeenCalledTimes(2);
    expect(r.current.decisions.items[0]).toMatchObject({ id: "d1", status: "done" });
    expect(spies.goals).toHaveBeenCalledTimes(2);
    expect(spies.inbox).toHaveBeenCalledTimes(2);
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

describe("live group adapters", () => {
  const page = (id: number, priority: "now" | "next" | "later", delivered_at: string | null = null) => ({
    id, priority, delivered_at, text: "Please help", source: "board", created_at: "2026-11-11", dedup_key: null,
  });

  it("projects only pending now pages and retires delivered pages after a stale read", async () => {
    const pending = page(1, "now");
    spies.inbox.mockResolvedValue({ items: [pending, page(2, "next"), page(3, "later"), page(4, "now", "today")] });
    const r = mount({ sessionId: "elsewhere", thisChatOnly: true });
    await flush();
    const deliveries = () => workGroups(r.current, { thisChatOnly: true, sessionId: "elsewhere" })[0].items.filter((i) => i.kind === "delivery");
    expect(deliveries()).toHaveLength(1);
    expect(deliveries()[0].raw).toEqual(pending);
    spies.inbox.mockRejectedValue(new Error("offline"));
    emit("inbox.item");
    await flush();
    expect(r.current.activity.status).toBe("stale");
    expect(deliveries()).toHaveLength(1);
    spies.inbox.mockResolvedValue({ items: [page(1, "now", "today")] });
    r.current.activity.retry();
    await flush();
    expect(deliveries()).toHaveLength(0);
    expect(spies.activity).not.toHaveBeenCalled();
  });

  it("projects scheduled waits once and uses explicit task and wait ownership", async () => {
    const wait = { id: "wait:s2", prompt: "Continue", schedule: "2026-12-01T00:00:00Z", context_id: "s2", enabled: true };
    spies.schedules.mockResolvedValue({ jobs: [wait, { id: "regular", prompt: "Run", schedule: "0 9 * * *", enabled: true }], backend: "local" });
    spies.tasks.mockResolvedValue({ issues: [{ id: "owned", title: "Task", session_id: "s2", status: "open" }] });
    const r = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    const upcoming = workGroups(r.current)[2].items;
    expect(upcoming.filter((i) => i.id === wait.id).map((i) => i.kind)).toEqual(["wait"]);
    expect(upcoming.find((i) => i.id === wait.id)?.raw).toEqual(wait);
    expect(workGroups(r.current, { thisChatOnly: true, sessionId: "s2" })[2].items.map((i) => i.id)).toEqual(["owned", "wait:s2"]);
  });

  it("preserves explicit ownership for ordinary schedules in source and group filtering", async () => {
    const owned = { id: "one-shot", prompt: "Run", schedule: "2026-12-01T00:00:00Z", context_id: "s1", enabled: true };
    spies.schedules.mockResolvedValue({ jobs: [
      owned,
      { ...owned, id: "other", context_id: "s2" },
      { ...owned, id: "global", context_id: null },
    ], backend: "local" });
    const scoped = mount({ sessionId: "s1", thisChatOnly: true });
    const all = mount({ sessionId: "s1", thisChatOnly: false });
    await flush();
    expect(scoped.current.schedules.items.map((i) => i.id)).toEqual(["one-shot"]);
    expect(scoped.current.schedules.items[0]).toMatchObject({ sessionId: "s1", raw: owned });
    expect(scoped.current.schedules.groupItems?.map((i) => i.id)).toEqual(["one-shot"]);
    expect(workGroups(all.current, { thisChatOnly: true, sessionId: "s1" })[2].items
      .filter((i) => i.kind === "schedule").map((i) => i.id)).toEqual(["one-shot"]);
    expect(all.current.schedules.items.map((i) => i.id)).toEqual(["one-shot", "other", "global"]);
  });

  it("reconciles missed delivery, wait removal and new chats on reconnect", async () => {
    spies.inbox.mockResolvedValue({ items: [page(1, "now")] });
    spies.schedules.mockResolvedValue({ jobs: [{ id: "wait:old", prompt: "Resume", schedule: "2026-12-01T00:00:00Z" }], backend: "local" });
    const r = mount({ thisChatOnly: false });
    await flush();
    act(() => mocks.conn.forEach((fn) => fn(false)));
    spies.inbox.mockResolvedValue({ items: [page(2, "now")] });
    spies.schedules.mockResolvedValue({ jobs: [], backend: "local" });
    spies.sessions.mockResolvedValue({ sessions: [{ session_id: "new-chat", turn_count: 1, last_updated: null }] });
    spies.directives.mockResolvedValue({ directives: [dir("new", "in_force", "new-chat")], in_force: 1 });
    act(() => mocks.conn.forEach((fn) => fn(true)));
    await flush();
    const groups = workGroups(r.current);
    expect(groups[0].items.filter((i) => i.kind === "delivery").map((i) => i.id)).toEqual(["2"]);
    expect(groups[2].items.filter((i) => i.kind === "wait")).toEqual([]);
    expect(groups[3].items.map((i) => i.id)).toEqual(["new"]);
  });

  it("observes successful local directive cache refreshes without requiring a bus event", async () => {
    const r = mount({ sessionId: "local-cache", thisChatOnly: false });
    spies.directives.mockResolvedValue({ directives: [dir("local", "in_force", "local-cache")], in_force: 1 });
    await flush();
    expect(workGroups(r.current)[3].count).toBe(1);
    spies.directives.mockRejectedValue(new Error("offline"));
    await act(async () => { await directiveStore("local-cache").refresh(); });
    await flush();
    expect(r.current.directives.status).toBe("stale");
    expect(workGroups(r.current)[3].count).toBe(1);
    spies.directives.mockResolvedValue({ directives: [dir("local", "revoked", "local-cache")], in_force: 0 });
    await act(async () => { await directiveStore("local-cache").refresh(); });
    await flush();
    expect(workGroups(r.current)[3].count).toBe(0);
    expect(r.current.directives.items[0].raw).toMatchObject({ status: "revoked" });
  });

  it("retains inventory when discovery returns a read-failure reason and recovers on retry", async () => {
    spies.sessions.mockResolvedValue({ sessions: [{ session_id: "reason-other", turn_count: 1, last_updated: null }] });
    spies.directives.mockImplementation(async (id: string) => ({ directives: [dir(id, "in_force", id)], in_force: 1 }));
    const r = mount({ sessionId: "reason-focused", thisChatOnly: false });
    await flush();
    const loadedAt = r.current.directives.loadedAt;
    spies.sessions.mockResolvedValue({ sessions: [], reason: "Session index read failed" });
    emit("directive.changed");
    await flush();
    expect(r.current.directives.status).toBe("stale");
    expect(r.current.directives.loadedAt).toBe(loadedAt);
    expect(r.current.directives.error).toEqual(new Error("Session index read failed"));
    expect(r.current.directives.items.map((i) => i.id)).toEqual(["reason-focused", "reason-other"]);
    spies.sessions.mockResolvedValue({ sessions: [] });
    r.current.directives.retry();
    await flush();
    expect(r.current.directives.status).toBe("ready");
    expect(r.current.directives.items.map((i) => i.id)).toEqual(["reason-focused"]);
  });

  it("keeps successful initial ledgers across batches when another fails", async () => {
    const focused = "partial-focused";
    const others = Array.from({ length: 5 }, (_, i) => `partial-${i}`);
    spies.sessions.mockResolvedValue({ sessions: others.map((session_id) => ({ session_id, turn_count: 1, last_updated: null })) });
    const ledger = async (id: string) => ({ directives: [dir(id, "in_force", id)], in_force: 1 });
    spies.directives.mockImplementation(async (id: string) => {
      if (id === others[0]) throw new Error("Ledger unavailable");
      return ledger(id);
    });
    const r = mount({ sessionId: focused, thisChatOnly: false });
    await flush();
    expect(r.current.directives.status).toBe("stale");
    expect(r.current.directives.error).toBeInstanceOf(Error);
    expect(r.current.directives.items.map((i) => i.id)).toEqual([focused, ...others.slice(1)]);
    expect(workGroups(r.current, { thisChatOnly: true, sessionId: focused })[3].items.map((i) => i.id)).toEqual([focused]);
    spies.directives.mockImplementation(ledger);
    r.current.directives.retry();
    await flush();
    expect(r.current.directives.status).toBe("ready");
    expect(r.current.directives.error).toBeNull();
    expect(r.current.directives.items.map((i) => i.id)).toEqual([focused, ...others]);
    const loadedAt = r.current.directives.loadedAt;
    spies.directives.mockRejectedValue(new Error("Offline"));
    emit("directive.changed");
    await flush();
    expect(r.current.directives.status).toBe("stale");
    expect(r.current.directives.loadedAt).toBe(loadedAt);
    expect(r.current.directives.items.map((i) => i.id)).toEqual([focused, ...others]);
  });

  it("reports error when no discovered ledger has ever loaded", async () => {
    spies.directives.mockRejectedValue(new Error("Offline"));
    const r = mount({ sessionId: "never-loaded-ledger", thisChatOnly: false });
    await flush();
    expect(r.current.directives.status).toBe("error");
    expect(r.current.directives.loadedAt).toBeNull();
    expect(r.current.directives.items).toEqual([]);
  });

  it("loads other chats with bounded fan-out and retries stale inventory", async () => {
    spies.sessions.mockResolvedValue({ sessions: Array.from({ length: 9 }, (_, i) => ({ session_id: `other-${i}`, turn_count: 1, last_updated: null })) });
    let active = 0;
    let maximum = 0;
    spies.directives.mockImplementation(async (id: string) => {
      active++;
      maximum = Math.max(maximum, active);
      await new Promise((resolve) => setTimeout(resolve, 0));
      active--;
      return { directives: [dir(id, "in_force", id)], in_force: 1 };
    });
    const r = mount({ sessionId: "focused", thisChatOnly: false });
    await flush();
    await flush();
    expect(r.current.directives.items).toHaveLength(10);
    expect(maximum).toBeLessThanOrEqual(4);
    expect(workGroups(r.current, { thisChatOnly: true, sessionId: "other-3" })[3].count).toBe(1);
    spies.sessions.mockRejectedValue(new Error("offline"));
    emit("directive.changed");
    await flush();
    expect(r.current.directives.status).toBe("stale");
    expect(r.current.directives.items).toHaveLength(10);
    spies.sessions.mockResolvedValue({ sessions: [] });
    r.current.directives.retry();
    await flush();
    expect(r.current.directives.items.map((i) => i.id)).toEqual(["focused"]);
  });
});

// #5094: a scheduled turn's stimulus is the scheduler's wake prompt. Its bracketed header
// is runtime framing, so the row title leads with the job's own prompt instead.
describe("activityTitle", () => {
  const entry = (over: Partial<ActivityEntry>): ActivityEntry => ({ ...actEntry(1, "completed"), ...over });

  it("drops the scheduler's wake header from the stimulus", () => {
    for (const header of [
      "[Autonomous wake — scheduled run. Orient from <working_state>, then:]",
      "[Autonomous wake — a watch you set has tripped. Orient from <working_state>, then:]",
      "[Autonomous wake — a wait you scheduled has elapsed. Continue:]",
      "[Autonomous wake — driving your active goal. Orient from <working_state>, then:]",
    ]) {
      expect(activityTitle(entry({ stimulus: `${header}\n\nCheck the board for blocked cards.` })))
        .toBe("Check the board for blocked cards.");
    }
  });

  it("keeps an ordinary stimulus and falls back past an empty one", () => {
    expect(activityTitle(entry({ stimulus: "CI webhook: build #4821 failed" }))).toBe("CI webhook: build #4821 failed");
    expect(activityTitle(entry({ stimulus: "[Autonomous wake — scheduled run. Orient from <working_state>, then:]" })))
      .toBe("activity 1");
    expect(activityTitle(entry({ stimulus: "", text: "" }))).toBe("job-1");
  });
});
