import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { closeDecisionNotice, closeSettledDecisionNotices } from "./closeDecisionNotices";
import { createDecisionsStore } from "../decisions/useDecisions";
import { decisionsApi, type DecisionRow } from "./api/decisions";

// The decision bus, driven by hand so the store hears decision.changed on demand.
const bus = vi.hoisted(() => ({
  listeners: new Map<string, Set<(d: Record<string, unknown>) => void>>(),
}));
vi.mock("./events", () => ({
  onTopic: (topic: string, fn: (d: Record<string, unknown>) => void) => {
    const set = bus.listeners.get(topic) ?? new Set();
    set.add(fn);
    bus.listeners.set(topic, set);
    return () => set.delete(fn);
  },
  onConnectionChange: (fn: (c: boolean) => void) => {
    fn(true);
    return () => {};
  },
}));
vi.mock("./notify", () => ({ notifyIfHidden: vi.fn() }));

type FakeNotice = { tag: string; close: ReturnType<typeof vi.fn>; timestamp?: number };

function notice(tag: string, timestamp?: number): FakeNotice {
  return { tag, close: vi.fn(), timestamp };
}

/** Stub navigator.serviceWorker with a registration holding `shown` (filtered by tag). */
function stubWorker(shown: FakeNotice[], getNotifications?: ReturnType<typeof vi.fn>) {
  const get = getNotifications ?? vi.fn(async (filter?: { tag?: string }) =>
    shown.filter((n) => !filter?.tag || n.tag === filter.tag));
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { ready: Promise.resolve({ getNotifications: get }) },
  });
  return get;
}

function decision(id: string, state: DecisionRow["state"] = "pending"): DecisionRow {
  return {
    id, notice_id: `decision-${id}`, session_id: "s1", agent: "Ava", question: `Q ${id}?`, options: [],
    created_at: "2026-11-23T00:00:00Z", state, answer: null, answered_at: null, expires_at: null,
    default_option: null, thread_id: "t", interrupt_id: "i", context: "", default_action: null,
    withdraw_reason: null, answered_by: null, answered_surface: null, default_action_text: null, answer_label: null,
  } as unknown as DecisionRow;
}

async function settle() {
  for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0));
}

function emit(topic: string, payload: Record<string, unknown>) {
  for (const fn of bus.listeners.get(topic) ?? []) fn(payload);
}

beforeEach(() => {
  bus.listeners.clear();
});

afterEach(() => {
  delete (navigator as { serviceWorker?: unknown }).serviceWorker;
  vi.restoreAllMocks();
});

describe("closeDecisionNotice", () => {
  it("closes only the named decision's notification", async () => {
    const d1 = notice("decision-d1");
    const d2 = notice("decision-d2");
    const get = stubWorker([d1, d2]);
    await closeDecisionNotice("d1");
    expect(get).toHaveBeenCalledWith({ tag: "decision-d1" });
    expect(d1.close).toHaveBeenCalledTimes(1);
    expect(d2.close).not.toHaveBeenCalled();
  });

  it("resolves without throwing when there is no service worker API", async () => {
    expect("serviceWorker" in navigator).toBe(false);
    await expect(closeDecisionNotice("d1")).resolves.toBeUndefined();
  });

  it("resolves without throwing when getNotifications rejects", async () => {
    stubWorker([], vi.fn().mockRejectedValue(new Error("denied")));
    await expect(closeDecisionNotice("d1")).resolves.toBeUndefined();
  });

  it("resolves without throwing when the registration has no getNotifications", async () => {
    Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: { ready: Promise.resolve({}) } });
    await expect(closeDecisionNotice("d1")).resolves.toBeUndefined();
  });
});

describe("closeSettledDecisionNotices", () => {
  it("keeps pending decisions, other notices and ones newer than the list read", async () => {
    const settled = notice("decision-d1", 100);
    const waiting = notice("decision-d2", 100);
    const other = notice("protoagent-notice", 100);
    const fresh = notice("decision-d3", 500);
    const get = stubWorker([settled, waiting, other, fresh]);
    await closeSettledDecisionNotices([decision("d2")], 200);
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith();
    expect(settled.close).toHaveBeenCalledTimes(1);
    expect(waiting.close).not.toHaveBeenCalled();
    expect(other.close).not.toHaveBeenCalled();
    expect(fresh.close).not.toHaveBeenCalled();
  });
});

describe("decisions store closes settled notifications", () => {
  it.each(["answered", "cancelled"])("a decision.changed with state %s closes that notification", async (state) => {
    const d1 = notice("decision-d1");
    const d2 = notice("decision-d2");
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue({ decisions: [decision("d1"), decision("d2")], pending: 2 });
    const store = createDecisionsStore();
    const off = store.subscribe(() => {});
    await settle();
    const get = stubWorker([d1, d2]);
    vi.spyOn(decisionsApi, "getDecision").mockResolvedValue(decision("d1", state as DecisionRow["state"]));
    emit("decision.changed", { id: "d1", session_id: "s1", state });
    await settle();
    expect(get).toHaveBeenCalledWith({ tag: "decision-d1" });
    expect(d1.close).toHaveBeenCalled();
    expect(d2.close).not.toHaveBeenCalled();
    off();
  });

  it("a decision.changed with state pending closes nothing", async () => {
    const d1 = notice("decision-d1");
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue({ decisions: [decision("d1")], pending: 1 });
    const store = createDecisionsStore();
    const off = store.subscribe(() => {});
    await settle();
    stubWorker([d1]);
    emit("decision.changed", { id: "d1", session_id: "s1", state: "pending" });
    await settle();
    expect(d1.close).not.toHaveBeenCalled();
    off();
  });

  it("on start closes an answered decision's notification and keeps a pending one's", async () => {
    const d1 = notice("decision-d1");
    const d2 = notice("decision-d2");
    stubWorker([d1, d2]);
    // d1 was answered elsewhere, so the pending list holds only d2.
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue({ decisions: [decision("d2")], pending: 1 });
    const store = createDecisionsStore();
    const off = store.subscribe(() => {});
    await settle();
    expect(d1.close).toHaveBeenCalledTimes(1);
    expect(d2.close).not.toHaveBeenCalled();
    off();
  });

  it("coming back to the front sweeps against the held list without a new read", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue({ decisions: [decision("d2")], pending: 1 });
    const store = createDecisionsStore();
    const off = store.subscribe(() => {});
    await settle();
    const d1 = notice("decision-d1", 0);
    const d2 = notice("decision-d2", 0);
    stubWorker([d1, d2]);
    const reads = list.mock.calls.length;
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    expect(d1.close).toHaveBeenCalledTimes(1);
    expect(d2.close).not.toHaveBeenCalled();
    expect(list.mock.calls.length).toBe(reads);
    off();
  });

  it("a failed list read closes nothing", async () => {
    const d1 = notice("decision-d1");
    stubWorker([d1]);
    vi.spyOn(decisionsApi, "listDecisions").mockRejectedValue(new Error("offline"));
    const store = createDecisionsStore();
    const off = store.subscribe(() => {});
    await settle();
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    expect(d1.close).not.toHaveBeenCalled();
    off();
  });
});
