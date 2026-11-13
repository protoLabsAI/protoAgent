// ADR 0130 S5 (r3): the rail's decision pill (useDecisions) and Work's "Needs you" decision
// count (useWorkItems) read different endpoints — `GET /api/decisions?state=pending` vs the
// full list — but must never disagree. Both move on the same `decision.changed`, and both
// re-read on a `resync_required` cursor gap (D3). Driven through one faked bus + spied decisions
// API so a divergence would surface as a count mismatch.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const bus = vi.hoisted(() => ({ listeners: new Map<string, Set<(d: Record<string, unknown>) => void>>() }));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (d: Record<string, unknown>) => void) => {
    const set = bus.listeners.get(topic) ?? new Set();
    set.add(fn);
    bus.listeners.set(topic, set);
    return () => set.delete(fn);
  },
}));
vi.mock("../lib/notify", () => ({ notifyIfHidden: vi.fn() }));

import { api } from "../lib/api";
import { decisionsApi, type DecisionRow } from "../lib/api/decisions";
import { useDecisions } from "../decisions/useDecisions";
import { useWorkItems } from "./useWorkItems";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const emit = (topic: string, data: Record<string, unknown> = {}) =>
  act(() => bus.listeners.get(topic)?.forEach((fn) => fn(data)));

const row = (id: string, state: DecisionRow["state"]): DecisionRow => ({
  id, session_id: "s1", agent: "proto", question: `q ${id}`, options: [],
  created_at: "2026-11-09T00:00:00+00:00", state, answer: null, answered_at: null,
  expires_at: null, default_option: null, thread_id: "", interrupt_id: "", context: "",
  default_action: null, withdraw_reason: null, answered_by: null, answered_surface: null,
  default_action_text: "", answer_label: null,
});

let all: DecisionRow[] = [];
let roots: Root[] = [];

beforeEach(() => {
  bus.listeners.clear();
  all = [row("a", "pending")];
  // The pending endpoint the rail reads vs. the full list Work reads, both off one source.
  vi.spyOn(decisionsApi, "listDecisions").mockImplementation(async (state) => {
    const pending = all.filter((d) => d.state === "pending");
    return { decisions: state === "pending" ? pending : all, pending: pending.length };
  });
  vi.spyOn(decisionsApi, "getDecision").mockImplementation(async (id) => all.find((d) => d.id === id) ?? row(id, "answered"));
  vi.spyOn(api, "goals").mockResolvedValue({ goals: [], enabled: true });
  vi.spyOn(api, "watches").mockResolvedValue({ watches: [], enabled: true });
  vi.spyOn(api, "tasks").mockResolvedValue({ issues: [] });
  vi.spyOn(api, "schedules").mockResolvedValue({ jobs: [], backend: "local" });
  vi.spyOn(api, "background").mockResolvedValue({ enabled: true, jobs: [] });
  vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
  vi.spyOn(api, "chatSessions").mockResolvedValue({ sessions: [] });
  vi.spyOn(api, "listDirectives").mockResolvedValue({ directives: [], in_force: 0 });
});
afterEach(() => {
  roots.forEach((r) => act(() => r.unmount()));
  roots = [];
  vi.restoreAllMocks();
});

type Counts = { rail: number; work: number };
function mount(): { current: Counts } {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  roots.push(root);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
  const result = { current: { rail: -1, work: -1 } as Counts };
  function Probe() {
    const { pendingCount } = useDecisions();
    const { decisions } = useWorkItems({ thisChatOnly: false });
    result.current = { rail: pendingCount, work: decisions.items.filter((i) => i.status === "needs-you").length };
    return null;
  }
  act(() => root.render(h(QueryClientProvider, { client }, h(Probe))));
  return result;
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));
  });
}

describe("decision counts stay consistent between the rail and Work (r3)", () => {
  it("both move together on decision.changed and on a resync_required gap", async () => {
    const r = mount();
    await flush();
    expect(r.current).toEqual({ rail: 1, work: 1 });

    // A second decision is raised elsewhere: both counts climb to 2.
    all = [row("a", "pending"), row("b", "pending")];
    await emit("decision.changed", { id: "b", session_id: "s1", state: "pending" });
    await flush();
    expect(r.current).toEqual({ rail: 2, work: 2 });

    // One is answered on another device: both drop to 1 on the same event.
    all = [row("a", "answered"), row("b", "pending")];
    await emit("decision.changed", { id: "a", session_id: "s1", state: "answered" });
    await flush();
    expect(r.current).toEqual({ rail: 1, work: 1 });

    // A cursor gap past retention re-reads both sources; they converge, still equal.
    all = [row("b", "pending")];
    await emit("resync_required");
    await flush();
    expect(r.current.rail).toBe(r.current.work);
    expect(r.current).toEqual({ rail: 1, work: 1 });
  });
});
