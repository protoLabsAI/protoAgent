import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DecisionCard } from "./DecisionCard";
import { DecisionsPanel, DecisionsSheet } from "./DecisionsPanel";
import { createDecisionsStore, useDecision, type DecisionLookup } from "./useDecisions";
import { decisionsApi, type DecisionRow } from "../lib/api/decisions";
import { ApiError } from "../lib/api/http";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The decision bus + the native-notification seam, mocked so the store is driven by hand.
const bus = vi.hoisted(() => ({
  listeners: new Map<string, Set<(d: Record<string, unknown>) => void>>(),
  connections: new Set<(c: boolean) => void>(),
  topics: [] as string[],
}));
const mocks = vi.hoisted(() => ({ notify: vi.fn() }));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (d: Record<string, unknown>) => void) => {
    bus.topics.push(topic);
    const set = bus.listeners.get(topic) ?? new Set();
    set.add(fn);
    bus.listeners.set(topic, set);
    return () => set.delete(fn);
  },
  onConnectionChange: (fn: (c: boolean) => void) => {
    bus.connections.add(fn);
    fn(true); // the store ignores the subscribe-time connected callback (no offline edge yet)
    return () => bus.connections.delete(fn);
  },
}));
vi.mock("../lib/notify", () => ({ notifyIfHidden: mocks.notify }));

/** Fire one bus topic's listeners by hand (the store is driven, not transported). */
const emit = (topic: string, data: Record<string, unknown> = {}) =>
  bus.listeners.get(topic)?.forEach((fn) => fn(data));
/** Drive the connection state (an offline→online edge re-reads the list, ADR 0130 D3). */
const setOnline = (online: boolean) => bus.connections.forEach((fn) => fn(online));

const decision = (id: string, patch: Partial<DecisionRow> = {}): DecisionRow => ({
  id,
  session_id: "s1",
  agent: "protoEngineer",
  question: "Merge the release?",
  options: [
    { id: "merge", label: "Merge with the WARN", description: "I'll merge and cut the release.", recommended: true },
    { id: "hold", label: "Hold the release" },
  ],
  created_at: "2026-11-08T14:45:00+00:00",
  state: "pending",
  answer: null,
  answered_at: null,
  expires_at: null,
  default_option: null,
  thread_id: "t1",
  interrupt_id: "i1",
  context: "",
  default_action: null,
  withdraw_reason: null,
  answered_by: null,
  answered_surface: null,
  default_action_text: "carry on without this step",
  answer_label: null,
  ...patch,
});
const listOf = (decisions: DecisionRow[]) => ({ decisions, pending: decisions.length });
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

let cleanup: (() => void)[] = [];
let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.restoreAllMocks();
  bus.listeners.clear();
  bus.connections.clear();
  bus.topics.length = 0;
  mocks.notify.mockClear();
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  cleanup = [];
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  vi.useRealTimers();
  cleanup.forEach((off) => off());
  await act(async () => root.unmount());
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderNode(node: ReactNode) {
  await act(async () => {
    root.render(node);
  });
}
const buttons = () => [...document.querySelectorAll<HTMLButtonElement>("button")];
async function click(label: string) {
  const btn = buttons().find((b) => b.textContent?.includes(label) && !b.disabled);
  if (!btn) throw new Error(`no enabled button matching ${label}`);
  await act(async () => {
    btn.click();
  });
  await act(async () => {
    await Promise.resolve();
  });
}

function mountStore() {
  const store = createDecisionsStore();
  cleanup.push(store.subscribe(() => {}));
  return store;
}

// ── useDecisions store ────────────────────────────────────────────────────────────────────
describe("useDecisions store", () => {
  it("keeps the server order, exposes top/pendingCount, hidden until first load", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a"), decision("b")]));
    const store = mountStore();
    expect(store.getSnapshot().loaded).toBe(false);
    expect(store.getSnapshot().state).toBe("loading");
    await settle();
    expect(store.getSnapshot().decisions.map((d) => d.id)).toEqual(["a", "b"]);
    expect(store.getSnapshot().pendingCount).toBe(2);
    expect(store.getSnapshot().top?.id).toBe("a");
    expect(store.getSnapshot().loaded).toBe(true);
    expect(store.getSnapshot().state).toBe("ready");
  });

  it("refreshes on decision.changed, on reconnect, and on resync_required, and never polls", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    mountStore();
    await settle();
    expect(list).toHaveBeenCalledTimes(1);
    expect(bus.topics).toContain("decision.changed");
    expect(bus.topics).toContain("resync_required");

    emit("decision.changed", { id: "a", session_id: "s1", state: "pending" });
    await settle();
    expect(list).toHaveBeenCalledTimes(2);

    // A drop never refreshes; the offline→online reconnect edge re-reads once, because a fresh
    // stream with no resume cursor replays nothing (ADR 0130 D3). Not the subscribe-time
    // connected callback — only a real offline→online edge.
    setOnline(false);
    expect(list).toHaveBeenCalledTimes(2);
    setOnline(true);
    await settle();
    expect(list).toHaveBeenCalledTimes(3);

    // A cursor gap past retention sends resync_required, which also forces a plain re-read.
    emit("resync_required");
    await settle();
    expect(list).toHaveBeenCalledTimes(4);
  });

  it("reconciles departed cached pending records on reconnect and does not reread unchanged or terminal records", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("departed"), decision("waiting")]));
    const get = vi.spyOn(decisionsApi, "getDecision").mockResolvedValue(decision("historical", { state: "answered" }));
    const store = mountStore();
    await settle();
    await store.loadDecision("historical");
    get.mockClear();
    setOnline(false);
    list.mockResolvedValue(listOf([decision("waiting")]));
    const terminal = decision("departed", { state: "expired" });
    let resolve!: (row: DecisionRow) => void;
    get.mockImplementation(() => new Promise((done) => { resolve = done; }));
    setOnline(true);
    await settle();
    expect(get).toHaveBeenCalledExactlyOnceWith("departed");
    // Keep the old list until its departed record has been reconciled.
    expect(store.getSnapshot().decisions.map((row) => row.id)).toEqual(["departed", "waiting"]);
    resolve(terminal);
    await settle();
    expect(store.getSnapshot().records.departed).toEqual(terminal);
    expect(store.getSnapshot().decisions.map((row) => row.id)).toEqual(["waiting"]);
    expect(store.getSnapshot().pendingCount).toBe(1);
    await store.refresh();
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("notifies once per new pending id, with a click that opens the panel", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    const store = mountStore();
    await settle();
    expect(mocks.notify).not.toHaveBeenCalled();

    list.mockResolvedValue(listOf([decision("dec-1")]));
    emit("decision.changed", { id: "dec-1", session_id: "s1", state: "pending" });
    await settle();
    expect(mocks.notify).toHaveBeenCalledTimes(1);
    const [title, body, onClick] = mocks.notify.mock.calls[0];
    expect(title).toBe("protoEngineer needs a decision");
    expect(body).toBe("Merge the release?");
    expect(onClick).toBe(store.openPanel);

    expect(store.getSnapshot().panelOpen).toBe(false);
    (onClick as () => void)();
    expect(store.getSnapshot().panelOpen).toBe(true);

    // A reload with the same id does not notify again (§4.5).
    emit("decision.changed", { id: "dec-1", session_id: "s1", state: "pending" });
    await settle();
    expect(mocks.notify).toHaveBeenCalledTimes(1);
  });

  it("opens and closes the panel from outside", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    const store = mountStore();
    await settle();
    expect(store.getSnapshot().panelOpen).toBe(false);
    store.openPanel();
    expect(store.getSnapshot().panelOpen).toBe(true);
    store.closePanel();
    expect(store.getSnapshot().panelOpen).toBe(false);
  });

  it("answers via the API with the console surface, and returns a 409 as answered-elsewhere", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a")]));
    const answer = vi.spyOn(decisionsApi, "answerDecision");
    const store = mountStore();
    await settle();

    answer.mockResolvedValue({ answered_elsewhere: false, decision: decision("a", { state: "answered" }), resumed: true });
    const ok = await store.answer("a", "merge");
    expect(answer).toHaveBeenCalledWith("a", { option_id: "merge", surface: "console" });
    expect(ok).toMatchObject({ answered_elsewhere: false, resumed: true });

    answer.mockResolvedValue({ answered_elsewhere: true, decision: decision("a", { state: "answered" }) });
    const conflict = await store.answer("a", "merge");
    expect(conflict.answered_elsewhere).toBe(true);
  });

  it("returns sent immediately while a stale list refresh is still in flight", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a")]));
    const store = mountStore();
    await settle();
    let finish!: (value: ReturnType<typeof listOf>) => void;
    list.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const refreshing = store.refresh();
    vi.spyOn(decisionsApi, "answerDecision").mockResolvedValue({
      answered_elsewhere: false, resumed: true, decision: decision("a", { state: "answered" }),
    });
    expect(await store.answer("a", "merge")).toMatchObject({ resumed: true });
    expect(store.getSnapshot().pendingCount).toBe(0);
    list.mockResolvedValue(listOf([decision("a")]));
    finish(listOf([decision("a")]));
    await refreshing;
    expect(store.getSnapshot().pendingCount).toBe(0);
    expect(store.getSnapshot().records.a.state).toBe("answered");
  });

  // ── by-id record cache (#4725, Gap 2) ─────────────────────────────────────────────────────
  it("keeps an answered row in records after it leaves the pending list", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a")]));
    const answer = vi.spyOn(decisionsApi, "answerDecision");
    const store = mountStore();
    await settle();
    expect(store.getSnapshot().pendingCount).toBe(1);

    // The answer closes it, and the next pending list no longer carries it.
    const answered = decision("a", { state: "answered", answer_label: "Merge with the WARN" });
    answer.mockResolvedValue({ answered_elsewhere: false, decision: answered, resumed: true });
    list.mockResolvedValue(listOf([]));
    await store.answer("a", "merge");

    expect(store.getSnapshot().pendingCount).toBe(0);
    expect(store.getSnapshot().decisions).toEqual([]);
    // The settled record survives under its id for the source chat to read.
    expect(store.getSnapshot().records["a"].state).toBe("answered");
    expect(store.getSnapshot().records["a"].answer_label).toBe("Merge with the WARN");
  });

  it("loadAnswered merges the answered ledger into records, settling matching pending rows and clearing a 404 latch", async () => {
    const answeredA = decision("a", { state: "answered", answer_label: "Merge with the WARN" });
    const answeredH = decision("h", { state: "answered", answer_label: "Hold the release" });
    const list = vi.spyOn(decisionsApi, "listDecisions").mockImplementation(async (state = "pending") =>
      state === "answered" ? listOf([answeredA, answeredH, decision("ignored")]) : listOf([decision("a")]));
    vi.spyOn(decisionsApi, "getDecision").mockRejectedValue(new ApiError(404, "gone"));
    const store = mountStore();
    await settle();
    await store.loadDecision("h"); // latches h as missing via the 404
    expect(store.getSnapshot().missing["h"]).toBe(true);
    expect(store.getSnapshot().records["a"].state).toBe("pending");

    const first = store.loadAnswered();
    const second = store.loadAnswered(); // coalesces with the in-flight read
    await expect(first).resolves.toBe(true);
    await expect(second).resolves.toBe(true);
    expect(list.mock.calls.filter(([state]) => state === "answered")).toHaveLength(1);

    // Answered rows land in the cache; the one that was pending here leaves the pending list.
    expect(store.getSnapshot().records["a"].state).toBe("answered");
    expect(store.getSnapshot().pendingCount).toBe(0);
    expect(store.getSnapshot().decisions).toEqual([]);
    // The server re-listing h as answered clears the 404 latch.
    expect(store.getSnapshot().records["h"].state).toBe("answered");
    expect(store.getSnapshot().missing["h"]).toBeUndefined();
    // A non-answered row in the payload never lands — a stray pending copy must not clobber.
    expect(store.getSnapshot().records["ignored"]).toBeUndefined();
  });

  it("a failed answered-ledger read leaves the cache unchanged and the next call retries", async () => {
    const list = vi.spyOn(decisionsApi, "listDecisions").mockImplementation(async (state = "pending") => {
      if (state === "answered") throw new Error("down");
      return listOf([]);
    });
    const store = mountStore();
    await settle();
    await expect(store.loadAnswered()).resolves.toBe(false);
    expect(store.getSnapshot().records).toEqual({});

    const answered = decision("a", { state: "answered" });
    list.mockImplementation(async (state = "pending") => listOf(state === "answered" ? [answered] : []));
    await expect(store.loadAnswered()).resolves.toBe(true);
    expect(store.getSnapshot().records["a"].state).toBe("answered");
  });

  it("re-reads a cached id that left the pending list on decision.changed, even one still cached as pending", async () => {
    // "a" starts pending and is cached by the first list. Another member then answers it: it
    // leaves the pending list (so the refresh can't update it) and the event names it. A cached
    // id that has left the pending list is re-read by a by-id GET even when it is still cached as
    // pending, so the stale pending record is replaced rather than kept.
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a")]));
    const get = vi
      .spyOn(decisionsApi, "getDecision")
      .mockResolvedValue(decision("a", { state: "answered", answer_label: "Merge with the WARN" }));
    const store = mountStore();
    await settle();
    expect(store.getSnapshot().records["a"].state).toBe("pending");

    list.mockResolvedValue(listOf([]));
    emit("decision.changed", { id: "a", session_id: "s1", state: "answered" });
    await settle();
    await settle();

    expect(get).toHaveBeenCalledWith("a");
    expect(store.getSnapshot().records["a"].state).toBe("answered");
    expect(store.getSnapshot().records["a"].answer_label).toBe("Merge with the WARN");
    expect(store.getSnapshot().pendingCount).toBe(0);
    expect(store.getSnapshot().decisions).toEqual([]);
  });

  it("does not re-read on decision.changed when the id is still pending (the refresh covers it)", async () => {
    // The event names an id still in the refreshed pending list — the bulk refresh already carries
    // its latest row, so no extra by-id GET is issued.
    const list = vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a")]));
    const get = vi.spyOn(decisionsApi, "getDecision");
    const store = mountStore();
    await settle();
    expect(store.getSnapshot().records["a"].state).toBe("pending");

    list.mockResolvedValue(listOf([decision("a", { question: "Merge the release now?" })]));
    emit("decision.changed", { id: "a", session_id: "s1", state: "pending" });
    await settle();
    await settle();

    expect(get).not.toHaveBeenCalled();
    expect(store.getSnapshot().records["a"].question).toBe("Merge the release now?");
    expect(store.getSnapshot().pendingCount).toBe(1);
  });

  it("loadDecision fetches a historical id once by GET, then serves it from cache", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    const get = vi
      .spyOn(decisionsApi, "getDecision")
      .mockResolvedValue(decision("h", { state: "withdrawn", withdraw_reason: "I merged it myself." }));
    const store = mountStore();
    await settle();

    const row = await store.loadDecision("h");
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith("h");
    expect(row?.state).toBe("withdrawn");
    expect(store.getSnapshot().records["h"].withdraw_reason).toBe("I merged it myself.");

    // A second load is served from the cache — no second request.
    const again = await store.loadDecision("h");
    expect(again?.id).toBe("h");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("loadDecision re-reads a cached id still in state 'pending', then serves the settled row from cache", async () => {
    // A record cached as pending is not terminal: while no consumer is subscribed the store gets
    // no decision.changed events, so a re-mounted caller must re-read it rather than trust the
    // cached pending row. Once it comes back settled it is served from the cache with no request.
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("a")]));
    const get = vi
      .spyOn(decisionsApi, "getDecision")
      .mockResolvedValue(decision("a", { state: "answered", answer_label: "Merge with the WARN" }));
    const store = mountStore();
    await settle();
    expect(store.getSnapshot().records["a"].state).toBe("pending");

    const row = await store.loadDecision("a");
    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith("a");
    expect(row?.state).toBe("answered");
    expect(store.getSnapshot().records["a"].state).toBe("answered");
    expect(store.getSnapshot().records["a"].answer_label).toBe("Merge with the WARN");
    // The settled record is terminal: a second load is served from the cache, no further request.
    const again = await store.loadDecision("a");
    expect(again?.state).toBe("answered");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("coalesces two concurrent loads of the same id into one request", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    let resolve!: (row: DecisionRow) => void;
    const get = vi
      .spyOn(decisionsApi, "getDecision")
      .mockImplementation(() => new Promise<DecisionRow>((r) => (resolve = r)));
    const store = mountStore();
    await settle();

    const p1 = store.loadDecision("h");
    const p2 = store.loadDecision("h");
    expect(get).toHaveBeenCalledTimes(1);
    resolve(decision("h", { state: "answered" }));
    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1?.id).toBe("h");
    expect(r2?.id).toBe("h");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("records a 404 as missing and does not re-fetch it", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    const get = vi.spyOn(decisionsApi, "getDecision").mockRejectedValue(new ApiError(404, "not found"));
    const store = mountStore();
    await settle();

    const row = await store.loadDecision("gone");
    expect(row).toBeNull();
    expect(store.getSnapshot().missing["gone"]).toBe(true);
    expect(get).toHaveBeenCalledTimes(1);

    // Known-missing: a repeat load short-circuits without another request.
    const again = await store.loadDecision("gone");
    expect(again).toBeNull();
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("pendingCount counts only pending rows, never cached settled ones", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([decision("p")]));
    vi.spyOn(decisionsApi, "getDecision").mockResolvedValue(decision("s", { state: "answered" }));
    const store = mountStore();
    await settle();
    expect(store.getSnapshot().pendingCount).toBe(1);

    await store.loadDecision("s"); // a settled record enters the cache
    expect(store.getSnapshot().records["s"].state).toBe("answered");
    // The settled record is cached but is neither counted nor listed, and never notified.
    expect(store.getSnapshot().pendingCount).toBe(1);
    expect(store.getSnapshot().decisions.map((d) => d.id)).toEqual(["p"]);
    expect(mocks.notify).toHaveBeenCalledTimes(1);
  });

  it("useDecision loads a settled id on mount and resolves ready", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    vi.spyOn(decisionsApi, "getDecision").mockResolvedValue(
      decision("hook-ready", { state: "answered", answer_label: "Merge with the WARN" }),
    );
    let seen: DecisionLookup | null = null;
    function Probe() {
      seen = useDecision("hook-ready");
      return null;
    }
    await renderNode(<Probe />);
    await act(async () => {
      await settle();
    });
    expect(seen!.state).toBe("ready");
    expect(seen!.record?.answer_label).toBe("Merge with the WARN");
  });

  it("useDecision surfaces a 404 as state 'missing'", async () => {
    vi.spyOn(decisionsApi, "listDecisions").mockResolvedValue(listOf([]));
    vi.spyOn(decisionsApi, "getDecision").mockRejectedValue(new ApiError(404, "not found"));
    let seen: DecisionLookup | null = null;
    function Probe() {
      seen = useDecision("hook-gone");
      return null;
    }
    await renderNode(<Probe />);
    await act(async () => {
      await settle();
    });
    expect(seen!.state).toBe("missing");
    expect(seen!.record).toBeNull();
  });
});

// ── DecisionCard ──────────────────────────────────────────────────────────────────────────
describe("DecisionCard", () => {
  it("renders a pending decision: sm options, recommended primary + suggested, never danger", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} />);
    const merge = buttons().find((b) => b.textContent?.includes("Merge with the WARN"));
    expect(merge?.className).toContain("pl-btn--primary");
    expect(merge?.className).toContain("pl-btn--sm"); // desktop: compact (#5668 §2)
    const hold = buttons().find((b) => b.textContent?.trim() === "Hold the release");
    expect(hold?.className).not.toContain("pl-btn--primary");
    expect(buttons().some((b) => b.className.includes("pl-btn--danger"))).toBe(false);
    expect(document.body.textContent).toContain("suggested");
    expect(document.body.textContent).toContain("I'll merge and cut the release.");
    expect(document.body.textContent).toContain("Reply in chat instead");
  });

  // ── compact layout in a card container (#5668 spec v5 §1–§3, §6) ──────────────────────────
  const footerOf = () => document.querySelector<HTMLElement>("article footer");
  const answerGroup = () => document.querySelector('[role="group"][aria-label="Answer"]')!;

  it("keeps the options md at phone width, and an explicit actionSize still wins", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === "(max-width: 767px)", addEventListener() {}, removeEventListener() {},
    }));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} />);
    const option = () => buttons().find((b) => b.textContent?.trim() === "Hold the release")!;
    expect(option().className).not.toContain("pl-btn--sm");
    expect(buttons().find((b) => b.textContent?.trim() === "Dismiss")!.className).not.toContain("pl-btn--sm");
    await renderNode(<DecisionCard key="sized" decision={decision("a")} actionSize="sm" onAnswer={vi.fn()} />);
    expect(option().className).toContain("pl-btn--sm");
  });

  it("draws the DS Card edge on the article only when contained, keeping its role and name", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} />);
    const bare = document.querySelector("article")!;
    expect(bare.classList.contains("pl-card")).toBe(false); // Work detail / Needs you rail: host is the box
    expect(document.querySelector(".pl-card")).toBeNull();
    const name = bare.getAttribute("aria-label");
    expect(name).toMatch(/^protoEngineer: /);

    await renderNode(<DecisionCard key="contained" decision={decision("a")} contained onAnswer={vi.fn()} />);
    const boxed = document.querySelector("article")!;
    expect(boxed.classList.contains("pl-card")).toBe(true);
    expect(boxed.getAttribute("aria-label")).toBe(name);
    expect(document.querySelectorAll(".pl-card")).toHaveLength(1); // no box in a box
  });

  it("puts the secondary actions in the footer row, below and outside the Answer group", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} chatTitle="Cut v0.227.0" />);
    const footer = footerOf()!;
    const group = answerGroup();
    // The footer follows the options in the card.
    expect(group.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The Answer group holds the options only; each explanation stays attached to its option.
    const options = [...group.querySelectorAll("button")];
    expect(options.map((b) => b.querySelector("span")?.textContent)).toEqual(decision("a").options.map((o) => o.label));
    const described = options.find((b) => b.getAttribute("aria-describedby"))!;
    const desc = document.getElementById(described.getAttribute("aria-describedby")!)!;
    expect(desc.parentElement).toBe(described.parentElement);
    expect(desc.textContent).toBe(decision("a").options.find((o) => o.description)!.description);
    // One action row: Reply in chat instead · Open chat · Dismiss.
    const row = footer.querySelector("div")!;
    expect([...row.children].map((el) => el.textContent?.trim())).toEqual([
      "Reply in chat instead", "Open chat: Cut v0.227.0", "Dismiss",
    ]);
    expect([...row.querySelectorAll("button")].every((b) => b.className.includes("pl-btn--ghost"))).toBe(true);
    expect(footer.style.borderTop).toContain("var(--pl-color-border)");
  });

  it.each([true, false, undefined])("shows Reply in chat only when a pick is not required (requires_pick=%s)", async (requires_pick) => {
    await renderNode(
      <DecisionCard decision={decision("a", { requires_pick })} onAnswer={vi.fn()} onReplyInChat={vi.fn()} />,
    );
    expect(buttons().some((b) => b.textContent === "Reply in chat instead")).toBe(!requires_pick);
    expect(answerGroup().querySelectorAll("button")).toHaveLength(2);
    expect(footerOf()!.textContent).toContain("Open chat: protoEngineer");
    expect(buttons().some((b) => b.textContent === "Dismiss")).toBe(true);
  });

  it("states the expiry and default once, in the footer line", async () => {
    await renderNode(
      <DecisionCard
        decision={decision("a", { expires_at: "2026-11-08T15:45:00+00:00", default_option: "hold", default_action_text: "keep holding the release" })}
        onAnswer={vi.fn()}
      />,
    );
    const line = footerOf()!.querySelector("p")!;
    expect(line.textContent).toMatch(/^If nobody answers by .+, protoEngineer will keep holding the release\.$/);
    const text = document.querySelector("article")!.textContent!;
    expect(text.split("If nobody answers by")).toHaveLength(2);
    expect(text.split("keep holding the release")).toHaveLength(2);
    expect(answerGroup().textContent).not.toContain("keep holding the release");
  });

  it("drops the footer actions with the options, and the whole footer from a settled transcript card", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: true });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Hold the release");
    expect(footerOf()!.textContent).toBe("Open chat: protoEngineer"); // the panel keeps its way back to the chat
    await renderNode(
      <DecisionCard
        key="frozen"
        decision={decision("a", { state: "answered", answer_label: "Hold the release", answered_at: "2026-11-08T14:52:00+00:00" })}
        presentation="transcript"
        contained
        onAnswer={vi.fn()}
      />,
    );
    expect(footerOf()).toBeNull(); // no empty bordered strip
  });

  it("clamps a long question at 4 lines behind Show full question", async () => {
    vi.spyOn(Element.prototype, "scrollHeight", "get").mockReturnValue(240);
    vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(80);
    const long = Array.from({ length: 12 }, (_, i) => `Line ${i + 1} of a long question.`).join("\n");
    await renderNode(<DecisionCard decision={decision("a", { question: long })} onAnswer={vi.fn()} />);
    const question = () => [...document.querySelectorAll<HTMLElement>("article > p")].find((p) => p.textContent === long)!;
    expect(question().style.webkitLineClamp).toBe("4");
    expect(question().style.overflow).toBe("hidden");
    const toggle = buttons().find((b) => b.textContent?.trim() === "Show full question")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await act(async () => toggle.click());
    expect(question().style.webkitLineClamp).toBeFalsy();
    const less = buttons().find((b) => b.textContent?.trim() === "Show less")!;
    expect(less.getAttribute("aria-expanded")).toBe("true");
    await act(async () => less.click());
    expect(question().style.webkitLineClamp).toBe("4");
  });

  it("offers no Show full question when the question fits", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} />);
    expect(buttons().some((b) => b.textContent?.trim() === "Show full question")).toBe(false);
  });

  it("uses the muted text token for secondary text, never the subtle one", async () => {
    await renderNode(
      <DecisionCard
        decision={decision("a", { expires_at: "2026-11-08T15:45:00+00:00" })}
        contained
        onAnswer={vi.fn()}
      />,
    );
    const html = document.querySelector("article")!.outerHTML;
    expect(html).toContain("var(--pl-color-fg-muted)");
    expect(html).not.toContain("--pl-color-fg-subtle");
  });

  it("answering calls onAnswer and shows the Sent line; the panel holds the card", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: true });
    const onHold = vi.fn();
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} onHold={onHold} />);
    await click("Merge with the WARN");
    expect(onAnswer).toHaveBeenCalledWith("merge");
    expect(onHold).toHaveBeenCalled();
    expect(document.body.textContent).toContain('Sent: "Merge with the WARN"');
    expect(document.body.textContent).toContain("Continuing…");
  });

  it("shows 'answered elsewhere' (info callout) on a 409 result", async () => {
    const onAnswer = vi.fn().mockResolvedValue({
      answered_elsewhere: true,
      decision: decision("a", { state: "answered", answer_label: "Hold the release", answered_at: "2026-11-08T14:52:00+00:00" }),
    });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(document.body.textContent).toContain("Already answered");
    expect(document.body.textContent).toContain("Hold the release");
    expect(document.querySelector(".pl-callout--info")).toBeTruthy();
  });

  for (const presentation of ["panel", "transcript"] as const) {
    for (const state of ["expired", "withdrawn", "cancelled"] as const) {
      it(`${presentation}: a ${state} conflict uses the returned terminal record`, async () => {
        const onAnswer = vi.fn().mockResolvedValue({
          answered_elsewhere: true,
          decision: decision("a", {
            state,
            default_action_text: "paused deployment until tomorrow",
            withdraw_reason: "  The release was superseded.  ",
          }),
        });
        await renderNode(<DecisionCard decision={decision("a")} presentation={presentation} onAnswer={onAnswer} />);
        await click("Merge with the WARN");
        const result = host.querySelector('[aria-live="polite"][tabindex="-1"]');
        expect(result?.textContent).toBe(state === "expired"
          ? "No answer by the deadline, so protoEngineer went ahead and paused deployment until tomorrow."
          : "protoEngineer doesn't need this any more. The release was superseded.");
        expect(document.activeElement).toBe(result);
        expect(host.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
        expect(host.querySelector(".pl-callout--info")).toBeNull();
        expect(Boolean(host.querySelector(".pl-callout"))).toBe(presentation === "panel");
        expect(onAnswer).toHaveBeenCalledExactlyOnceWith("merge");
      });
    }
  }

  it("preserves the answered conflict fallback when the response has no record", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: true, decision: null });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(host.textContent).toContain('Already answered: you chose "an option". Nothing else changed.');
    expect(host.querySelector(".pl-callout--info")).toBeTruthy();
    expect(host.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
  });

  // ── delivery unknown (#5099): a failed request is not "nothing changed" ──
  // Flush the card's zero-delay first check and the promise it awaits.
  async function flushCheck(ms = 0) {
    await act(async () => {
      vi.advanceTimersByTime(ms);
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("checks before offering Try again when a send fails in transit", async () => {
    vi.useFakeTimers();
    const onAnswer = vi.fn().mockRejectedValue(new Error("network"));
    let resolveCheck: (row: DecisionRow) => void = () => {};
    const onCheck = vi.fn(() => new Promise<DecisionRow>((resolve) => { resolveCheck = resolve; }));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} onCheck={onCheck} />);
    await click("Merge with the WARN");
    // Unknown outcome: neutral information, no claim about what happened, nothing to retry.
    expect(host.textContent).toContain('Checking whether protoEngineer got "Merge with the WARN"…');
    expect(host.textContent).not.toContain("Couldn't send your answer");
    expect(host.textContent).not.toContain("Nothing changed");
    expect(host.querySelector(".pl-alert--warning")).toBeNull();
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(false);
    expect(host.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
    await flushCheck();
    expect(onCheck).toHaveBeenCalledExactlyOnceWith("a");
    // The server confirms it's still waiting with nothing on its way: now offer the retry.
    await act(async () => { resolveCheck(decision("a")); await Promise.resolve(); });
    expect(host.textContent).toContain("Couldn't send your answer");
    expect(host.querySelector(".pl-alert--warning")).toBeTruthy();
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(true);
    expect(buttons().find((b) => b.textContent?.includes("Merge with the WARN"))?.disabled).toBe(false);
    expect(buttons().find((b) => b.textContent?.trim() === "Hold the release")?.disabled).toBe(false);
    expect(onAnswer).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("shows Sent when a send that failed in transit had in fact landed", async () => {
    vi.useFakeTimers();
    const onAnswer = vi.fn().mockRejectedValue(new Error("timeout"));
    const onCheck = vi.fn().mockResolvedValue(decision("a", { state: "answered", answer: "merge", answer_label: "Merge with the WARN" }));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} onCheck={onCheck} />);
    await click("Merge with the WARN");
    await flushCheck();
    expect(host.textContent).toContain('Sent: "Merge with the WARN". Continuing…');
    expect(host.textContent).not.toContain("Couldn't send");
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(false);
    expect(onAnswer).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("keeps checking while the server can't be reached, never offering a retry", async () => {
    vi.useFakeTimers();
    const onAnswer = vi.fn().mockRejectedValue(new Error("offline"));
    const onCheck = vi.fn().mockRejectedValue(new Error("offline"));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} onCheck={onCheck} />);
    await click("Merge with the WARN");
    await flushCheck();
    expect(host.textContent).toContain('Can\'t confirm yet whether protoEngineer got "Merge with the WARN". Still checking…');
    expect(host.querySelector(".pl-alert--warning")).toBeNull();
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(false);
    await flushCheck(1999);
    expect(onCheck).toHaveBeenCalledTimes(1);
    await flushCheck(1);
    expect(onCheck).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("treats a 202 as on its way: no retry, then Sent once the agent takes it", async () => {
    vi.useFakeTimers();
    const onAnswer = vi.fn().mockResolvedValue({
      answered_elsewhere: false, decision: decision("a", { delivering: true }), resumed: false, delivering: true,
    });
    const onCheck = vi.fn()
      .mockResolvedValueOnce(decision("a", { delivering: true }))
      .mockResolvedValueOnce(decision("a", { state: "answered", answer: "merge" }));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} onCheck={onCheck} />);
    await click("Merge with the WARN");
    expect(host.textContent).toContain('Sent: "Merge with the WARN". Waiting for protoEngineer to pick it up…');
    expect(host.querySelector(".pl-alert--warning")).toBeNull();
    expect(host.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
    await flushCheck(1000);
    expect(onCheck).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("Waiting for protoEngineer to pick it up…");
    await flushCheck(1000);
    expect(onCheck).toHaveBeenCalledTimes(1); // backoff: the second check is 2s after the first
    await flushCheck(1000);
    expect(onCheck).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain('Sent: "Merge with the WARN". Continuing…');
    expect(onAnswer).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("settles a checking card from a live record update elsewhere", async () => {
    vi.useFakeTimers();
    const onAnswer = vi.fn().mockRejectedValue(new Error("network"));
    const onCheck = vi.fn(() => new Promise<DecisionRow>(() => {}));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} onCheck={onCheck} />);
    await click("Merge with the WARN");
    // Same card instance, new record: someone else's answer landed while we were checking.
    await renderNode(<DecisionCard decision={decision("a", { state: "answered", answer: "hold" })} onAnswer={onAnswer} onCheck={onCheck} />);
    expect(host.textContent).not.toContain("Checking whether");
    expect(host.textContent).toContain("Already answered");
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(false);
    vi.useRealTimers();
  });

  it("re-renders with a fresh onResolved do not extend the 4s Sent hold", async () => {
    vi.useFakeTimers();
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: true });
    const resolved = vi.fn();
    // Each render passes a NEW closure, as DecisionsPanel does.
    const render = () => renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} onResolved={(id) => resolved(id)} />);
    await render();
    await click("Merge with the WARN");
    expect(host.textContent).toContain("Sent:");
    for (let i = 0; i < 3; i++) {
      await act(async () => { vi.advanceTimersByTime(1000); });
      await render();
    }
    expect(resolved).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(1000); });
    expect(resolved).toHaveBeenCalledExactlyOnceWith("a");
  });

  // ── Dismiss (#5107): every pending card can be cleared from the console ──
  it("dismisses a pending card from the card itself", async () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn().mockResolvedValue(decision("a", { state: "withdrawn" }));
    const onResolved = vi.fn();
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} onDismiss={onDismiss} onResolved={onResolved} />);
    await click("Dismiss");
    expect(onDismiss).toHaveBeenCalledExactlyOnceWith("a");
    const result = host.querySelector('[aria-live="polite"][tabindex="-1"]');
    expect(result?.textContent).toBe("Dismissed. protoEngineer won't wait on this any more.");
    expect(document.activeElement).toBe(result);
    expect(host.querySelector(".pl-alert, .pl-callout--warning")).toBeNull();
    expect(host.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
    await act(async () => { vi.advanceTimersByTime(4000); });
    expect(onResolved).toHaveBeenCalledExactlyOnceWith("a");
  });

  it("says a dismissed card was already acted on when the server closed it first", async () => {
    const onDismiss = vi.fn().mockRejectedValue(new Error("409"));
    const onCheck = vi.fn().mockResolvedValue(decision("a", {
      state: "withdrawn", withdraw_reason: "It had already moved past this question, so there's nothing left to answer.",
    }));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} onDismiss={onDismiss} onCheck={onCheck} />);
    await click("Dismiss");
    expect(host.textContent).toContain("Dismissed. protoEngineer won't wait on this any more.");
    // A record that closed as answered meanwhile keeps the real outcome instead.
    onCheck.mockResolvedValue(decision("a", { state: "answered", answer: "hold", answer_label: "Hold the release" }));
    await renderNode(<DecisionCard key="b" decision={decision("a")} onAnswer={vi.fn()} onDismiss={onDismiss} onCheck={onCheck} />);
    await click("Dismiss");
    expect(host.textContent).toContain('Already answered: you chose "Hold the release"');
  });

  it("hides Dismiss only when the host passes onDismiss={null}", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} />);
    expect(buttons().some((b) => b.textContent?.trim() === "Dismiss")).toBe(true);
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} onDismiss={null} />);
    expect(buttons().some((b) => b.textContent?.trim() === "Dismiss")).toBe(false);
  });

  it("keeps Dismiss available, without an error alert, when it can't be confirmed", async () => {
    const onDismiss = vi.fn().mockRejectedValue(new Error("network"));
    const onCheck = vi.fn().mockResolvedValue(decision("a"));
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} onDismiss={onDismiss} onCheck={onCheck} />);
    await click("Dismiss");
    expect(host.textContent).toContain("Couldn't dismiss it just now. protoEngineer is still waiting, and you can try again.");
    expect(host.querySelector(".pl-alert")).toBeNull();
    expect(buttons().find((b) => b.textContent?.trim() === "Dismiss")?.disabled).toBe(false);
    onCheck.mockRejectedValue(new Error("offline"));
    await click("Dismiss");
    expect(host.textContent).toContain("Couldn't confirm the dismissal yet. You can try again in a moment.");
    // A 409 because an answer is mid-delivery says so, rather than inviting a retry.
    onCheck.mockResolvedValue(decision("a", { delivering: true }));
    await click("Dismiss");
    expect(host.textContent).toContain("An answer to this is already on its way to protoEngineer, so it can't be dismissed now.");
    expect(onDismiss).toHaveBeenCalledTimes(3);
  });

  it("closes a stranded card the server withdrew on answer (409) with the reason", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: true, decision: decision("a", {
      state: "withdrawn", withdraw_reason: "It had already moved past this question, so there's nothing left to answer.",
    }) });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(host.textContent).toContain(
      "protoEngineer doesn't need this any more. It had already moved past this question, so there's nothing left to answer.");
    expect(host.querySelector(".pl-alert")).toBeNull();
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(false);
  });

  it("drops the options and Try again when a failed send's decision settles elsewhere", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: false });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(document.body.textContent).toContain("Couldn't send your answer");
    // Same card instance, new record: someone else answered while the error was showing.
    await renderNode(<DecisionCard decision={{ ...decision("a"), state: "withdrawn" }} onAnswer={onAnswer} />);
    expect(document.body.textContent).not.toContain("Couldn't send your answer");
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(false);
    expect(document.querySelector('[role="group"][aria-label="Answer"]')).toBeNull();
  });

  it("treats a 200 with resumed=false as a failed send, not a Sent success", async () => {
    // Delivery to the parked turn failed; the decision is still pending server-side. The card
    // must NOT show "Sent" — it keeps the options enabled and offers Try again (§4.4).
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: false });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(document.body.textContent).not.toContain("Sent:");
    expect(document.body.textContent).toContain("Couldn't send your answer");
    expect(document.querySelector(".pl-alert--warning")).toBeTruthy();
    expect(buttons().some((b) => b.textContent?.trim() === "Try again")).toBe(true);
    expect(buttons().find((b) => b.textContent?.includes("Merge with the WARN"))?.disabled).toBe(false);
    expect(buttons().find((b) => b.textContent?.trim() === "Hold the release")?.disabled).toBe(false);
  });

  it("renders the expired state as a neutral callout with the default action", async () => {
    await renderNode(
      <DecisionCard
        decision={decision("a", {
          state: "expired",
          expires_at: "2026-11-08T15:45:00+00:00",
          default_option: "hold",
          default_action: "keep holding the release",
          default_action_text: "keep holding the release",
        })}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain("went ahead and keep holding the release");
    expect(document.querySelector(".pl-callout")).toBeTruthy();
    expect(document.querySelector(".pl-callout--info")).toBeFalsy();
    expect(buttons().some((b) => b.textContent?.includes("Merge with the WARN"))).toBe(false);
  });

  it("renders the no-longer-needed state with the agent's reason", async () => {
    await renderNode(
      <DecisionCard decision={decision("a", { state: "withdrawn", withdraw_reason: "I merged it myself." })} onAnswer={vi.fn()} />,
    );
    expect(document.body.textContent).toContain("protoEngineer doesn't need this any more.");
    expect(document.body.textContent).toContain("I merged it myself.");
    expect(document.querySelector(".pl-callout--info")).toBeFalsy();
  });

  it("agent offline, backend holds: neutral dot, line, options stay enabled", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} agentOnline={false} />);
    expect(document.body.textContent).toContain("protoEngineer is offline. It gets your answer when it starts again.");
    expect(document.querySelector(".pl-dot")).toBeTruthy();
    expect(buttons().find((b) => b.textContent?.includes("Merge with the WARN"))?.disabled).toBe(false);
  });

  it("agent offline, no hold: options disabled, Start action", async () => {
    const onStart = vi.fn();
    await renderNode(
      <DecisionCard decision={decision("a")} onAnswer={vi.fn()} agentOnline={false} agentHoldsOffline={false} onStartAgent={onStart} />,
    );
    expect(document.body.textContent).toContain("protoEngineer is offline. Start it to answer.");
    expect(buttons().find((b) => b.textContent?.includes("Merge with the WARN"))?.disabled).toBe(true);
    await act(async () => {
      buttons().find((b) => b.textContent?.trim() === "Start")!.click();
    });
    expect(onStart).toHaveBeenCalled();
  });

  it("shows the fallback-action line only with an expiry, and the Open chat link", async () => {
    const onOpenChat = vi.fn();
    await renderNode(
      <DecisionCard
        decision={decision("a", { expires_at: "2026-11-08T15:45:00+00:00", default_option: "hold", default_action_text: "keep holding the release" })}
        onAnswer={vi.fn()}
        onOpenChat={onOpenChat}
        chatTitle="Cut v0.227.0"
      />,
    );
    expect(document.body.textContent).toContain("If nobody answers by");
    expect(document.body.textContent).toContain("protoEngineer will keep holding the release");
    const link = [...document.querySelectorAll("a")].find((a) => a.textContent?.includes("Open chat"));
    expect(link?.textContent).toContain("Cut v0.227.0");
    await act(async () => link!.click());
    expect(onOpenChat).toHaveBeenCalled();
  });

  it("has no Open chat link in the source chat's own transcript", async () => {
    await renderNode(
      <DecisionCard decision={decision("a")} presentation="transcript" onAnswer={vi.fn()} onOpenChat={vi.fn()} chatTitle="mythx" />,
    );
    expect([...document.querySelectorAll("a")].some((a) => a.textContent?.includes("Open chat"))).toBe(false);
  });

  it("hides the fallback-action line when there is no expiry", async () => {
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={vi.fn()} />);
    expect(document.body.textContent).not.toContain("If nobody answers");
  });

  // ── transcript presentation (#4725, §5.5–5.6) ─────────────────────────────────────────────
  it("transcript: a newly answered card freezes on Sent and never calls onResolved", async () => {
    vi.useFakeTimers();
    try {
      const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: true });
      const onResolved = vi.fn();
      await renderNode(
        <DecisionCard decision={decision("a")} presentation="transcript" onAnswer={onAnswer} onResolved={onResolved} />,
      );
      await click("Merge with the WARN");
      expect(document.body.textContent).toContain('Sent: "Merge with the WARN"');
      // Past the panel's 4s hold: the transcript card stays put and never resolves itself.
      await act(async () => {
        vi.advanceTimersByTime(8000);
      });
      expect(onResolved).not.toHaveBeenCalled();
      expect(document.body.textContent).toContain('Sent: "Merge with the WARN"');
    } finally {
      vi.useRealTimers();
    }
  });

  it("transcript: once the answered record lands, the card freezes as the 'You chose' line", async () => {
    const onResolved = vi.fn();
    const answered = decision("a", {
      state: "answered",
      answer_label: "Merge with the WARN",
      answered_at: "2026-11-08T14:52:00+00:00",
    });
    await renderNode(
      <DecisionCard decision={answered} presentation="transcript" onAnswer={vi.fn()} onResolved={onResolved} />,
    );
    expect(document.body.textContent).toContain('You chose "Merge with the WARN" ·');
    // No options and no "Already answered" callout — just the frozen line.
    expect(buttons().some((b) => b.textContent?.includes("Merge with the WARN"))).toBe(false);
    expect(buttons().some((b) => b.textContent?.trim() === "Hold the release")).toBe(false);
    expect(document.body.textContent).not.toContain("Already answered");
    expect(document.querySelector(".pl-callout")).toBeFalsy();
    expect(onResolved).not.toHaveBeenCalled();
  });

  it("transcript: a typed answer freezes as 'You replied in chat', an option or old record as 'You chose'", async () => {
    const answered = (patch: Partial<DecisionRow>) => decision("a", {
      state: "answered",
      answer: "merge",
      answer_label: "Merge with the WARN",
      answered_at: "2026-11-08T14:52:00+00:00",
      ...patch,
    });
    await renderNode(
      <DecisionCard
        decision={answered({ answer_kind: "text", answer: "Wait for the fix", answer_label: "Wait for the fix" })}
        presentation="transcript"
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain("You replied in chat ·");
    // The reply is the user bubble below the card: the line does not repeat it.
    expect(document.body.textContent).not.toContain("Wait for the fix");
    expect(document.body.textContent).not.toContain("You chose");
    expect(document.querySelector(".pl-callout")).toBeFalsy();

    for (const answer_kind of ["option", null, undefined] as const) {
      await renderNode(
        <DecisionCard key={String(answer_kind)} decision={answered({ answer_kind })} presentation="transcript" onAnswer={vi.fn()} />,
      );
      expect(document.body.textContent).toContain('You chose "Merge with the WARN" ·');
      expect(document.body.textContent).not.toContain("You replied in chat");
    }
  });

  it("panel: a typed answer reads 'Already answered in chat' with the text cut at 80 characters", async () => {
    const typed = `${"a".repeat(80)}TAIL`;
    await renderNode(
      <DecisionCard
        decision={decision("a", {
          state: "answered",
          answer: typed,
          answer_label: typed,
          answer_kind: "text",
          answered_at: "2026-11-08T14:52:00+00:00",
        })}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain(`Already answered in chat: "${"a".repeat(80)}…" at `);
    expect(document.body.textContent).toContain("Nothing else changed.");
    expect(document.body.textContent).not.toContain("TAIL");
    expect(document.body.textContent).not.toContain("you chose");
    expect(document.querySelector(".pl-callout--info")).toBeTruthy();

    // A short typed answer is quoted whole, with no ellipsis.
    await renderNode(
      <DecisionCard
        key="short"
        decision={decision("a", { state: "answered", answer: "Wait for the fix", answer_kind: "text", answered_at: "2026-11-08T14:52:00+00:00" })}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain('Already answered in chat: "Wait for the fix" at ');
  });

  it("panel: a 409 carrying a typed answer shows the in-chat callout; an option keeps 'you chose'", async () => {
    const onAnswer = vi.fn().mockResolvedValue({
      answered_elsewhere: true,
      decision: decision("a", { state: "answered", answer: "Wait for the fix", answer_label: "Wait for the fix", answer_kind: "text", answered_at: "2026-11-08T14:52:00+00:00" }),
    });
    await renderNode(<DecisionCard decision={decision("a")} onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(document.body.textContent).toContain('Already answered in chat: "Wait for the fix" at ');

    await renderNode(
      <DecisionCard
        key="option"
        decision={decision("a", { state: "answered", answer: "hold", answer_label: "Hold the release", answer_kind: "option", answered_at: "2026-11-08T14:52:00+00:00" })}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain('Already answered: you chose "Hold the release" at ');
  });

  it("panel: the 80-character cut never splits an emoji", async () => {
    const typed = `${"a".repeat(79)}😀TAIL`;
    await renderNode(
      <DecisionCard
        decision={decision("a", { state: "answered", answer: typed, answer_kind: "text", answered_at: "2026-11-08T14:52:00+00:00" })}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain(`Already answered in chat: "${"a".repeat(79)}😀…" at `);
    expect(document.body.textContent).not.toContain("TAIL");
  });

  // `delivering` says an answer is on its way, not which kind: an option picked in another tab
  // sets it too. The card therefore never claims a typed reply from it (#5668 review).
  it("a pending record with an answer in flight from elsewhere keeps its options and footer actions", async () => {
    vi.useFakeTimers();
    try {
      const onCheck = vi.fn();
      for (const presentation of ["panel", "transcript"] as const) {
        await renderNode(
          <DecisionCard
            key={presentation}
            decision={decision("a", { delivering: true, expires_at: "2026-11-08T15:45:00+00:00" })}
            presentation={presentation}
            onAnswer={vi.fn()}
            onCheck={onCheck}
          />,
        );
        expect(document.body.textContent).not.toContain("Sending your reply…");
        expect(document.querySelector('[role="group"][aria-label="Answer"]')).toBeTruthy();
        expect(buttons().some((b) => b.textContent?.trim() === "Dismiss")).toBe(true);
        expect(document.body.textContent).toContain("If nobody answers by");
      }
      // No background re-read loop starts from the flag alone.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000);
      });
      expect(onCheck).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("transcript: an expired record freezes on its closed line, not a callout", async () => {
    await renderNode(
      <DecisionCard
        decision={decision("a", {
          state: "expired",
          expires_at: "2026-11-08T15:45:00+00:00",
          default_action_text: "keep holding the release",
        })}
        presentation="transcript"
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain("went ahead and keep holding the release");
    expect(document.querySelector(".pl-callout")).toBeFalsy();
    expect(buttons().some((b) => b.textContent?.includes("Merge with the WARN"))).toBe(false);
  });

  it("transcript: a withdrawn record freezes on the no-longer-needed line with the reason", async () => {
    await renderNode(
      <DecisionCard
        decision={decision("a", { state: "withdrawn", withdraw_reason: "I merged it myself." })}
        presentation="transcript"
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain("protoEngineer doesn't need this any more.");
    expect(document.body.textContent).toContain("I merged it myself.");
    expect(document.querySelector(".pl-callout")).toBeFalsy();
  });

  it("transcript: a 409 freezes on the returned record's line, not the Already-answered callout", async () => {
    const onAnswer = vi.fn().mockResolvedValue({
      answered_elsewhere: true,
      decision: decision("a", { state: "answered", answer_label: "Hold the release", answered_at: "2026-11-08T14:52:00+00:00" }),
    });
    await renderNode(<DecisionCard decision={decision("a")} presentation="transcript" onAnswer={onAnswer} />);
    await click("Merge with the WARN");
    expect(document.body.textContent).toContain('You chose "Hold the release" ·');
    expect(document.body.textContent).not.toContain("Already answered");
    expect(document.querySelector(".pl-callout--info")).toBeFalsy();
  });

  it("transcript: a pending decision still renders its options (unchanged from the panel)", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ answered_elsewhere: false, decision: decision("a"), resumed: true });
    await renderNode(<DecisionCard decision={decision("a")} presentation="transcript" onAnswer={onAnswer} />);
    expect(buttons().some((b) => b.textContent?.includes("Merge with the WARN"))).toBe(true);
    // The source chat's card offers the free-text route again (#5678): typing does not answer on its own.
    expect(buttons().some((b) => b.textContent?.trim() === "Reply in chat instead")).toBe(true);
    expect(buttons().some((b) => b.textContent?.trim() === "Dismiss")).toBe(true);
    expect(document.body.textContent).not.toContain("You chose");
  });

  it("panel (default): an answered record still shows the Already-answered callout, not the frozen line", async () => {
    await renderNode(
      <DecisionCard
        decision={decision("a", { state: "answered", answer_label: "Merge with the WARN", answered_at: "2026-11-08T14:52:00+00:00" })}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.body.textContent).toContain("Already answered");
    expect(document.body.textContent).not.toContain("You chose");
    expect(document.querySelector(".pl-callout--info")).toBeTruthy();
  });
});

// ── DecisionsPanel / DecisionsSheet ─────────────────────────────────────────────────────────
describe("DecisionsPanel", () => {
  it("shows two skeleton blocks on first load", async () => {
    await renderNode(<DecisionsPanel decisions={[]} loading onAnswer={vi.fn()} />);
    expect(document.querySelector("[data-testid='decisions-loading']")).toBeTruthy();
    expect(document.querySelectorAll(".pl-skel-lines").length).toBe(2);
  });

  it("shows the nothing-waiting line when empty from the start", async () => {
    await renderNode(<DecisionsPanel decisions={[]} loading={false} onAnswer={vi.fn()} />);
    expect(document.body.textContent).toContain("Nothing is waiting on you. When an agent stops");
    expect(document.body.textContent).not.toContain("That's everything");
  });

  it("shows the That's-everything line after the last decision clears", async () => {
    await renderNode(<DecisionsPanel decisions={[decision("a")]} loading={false} onAnswer={vi.fn()} />);
    expect(document.body.textContent).toContain("Your agents stopped to ask you these.");
    await renderNode(<DecisionsPanel decisions={[]} loading={false} onAnswer={vi.fn()} />);
    expect(document.body.textContent).toContain("That's everything. Nothing is waiting on you.");
  });

  it("lists cards in order with a divider between them and no Popover", async () => {
    await renderNode(
      <DecisionsPanel
        decisions={[decision("a", { agent: "protoEngineer" }), decision("b", { agent: "Cindi" })]}
        loading={false}
        onAnswer={vi.fn()}
      />,
    );
    expect(document.querySelectorAll("article").length).toBe(2);
    expect(document.querySelectorAll(".pl-divider").length).toBe(1);
    // Each panel card has its own edge (#5668 §1).
    expect([...document.querySelectorAll("article")].every((el) => el.classList.contains("pl-card"))).toBe(true);
    expect(document.querySelector(".pl-popover")).toBeFalsy();
    expect(document.body.textContent).toContain("Cindi");
  });

  it("DecisionsSheet wraps the panel in a bottom Drawer titled 'Decisions waiting on you'", async () => {
    await renderNode(<DecisionsSheet open onClose={vi.fn()} decisions={[decision("a")]} loading={false} onAnswer={vi.fn()} />);
    expect(document.querySelector(".pl-drawer--bottom")).toBeTruthy();
    expect(document.body.textContent).toContain("Decisions waiting on you");
    expect(document.body.textContent).toContain("Your agents stopped to ask you these.");
  });
});

// ── decisions HTTP client ───────────────────────────────────────────────────────────────────
describe("decisions HTTP client", () => {
  it("lists pending decisions", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(listOf([decision("a")]))));
    vi.stubGlobal("fetch", fetch);
    const res = await decisionsApi.listDecisions("pending");
    expect(res.decisions[0].id).toBe("a");
    expect(String(fetch.mock.calls[0][0])).toContain("/api/decisions?state=pending");
  });

  it("answers via POST with { option_id, surface: console }", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ decision: decision("a", { state: "answered" }), resumed: true })));
    vi.stubGlobal("fetch", fetch);
    const res = await decisionsApi.answerDecision("a", { option_id: "merge" });
    expect(res).toMatchObject({ answered_elsewhere: false, resumed: true });
    const [url, init] = fetch.mock.calls[0];
    expect(String(url)).toContain("/api/decisions/a/answer");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ option_id: "merge", surface: "console" });
  });

  it("resolves a 409 as answered-elsewhere, carrying the closed record", async () => {
    const closed = decision("a", { state: "answered", answer_label: "Merge with the WARN" });
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: closed }), { status: 409 }));
    vi.stubGlobal("fetch", fetch);
    const res = await decisionsApi.answerDecision("a", { option_id: "merge" });
    expect(res.answered_elsewhere).toBe(true);
    expect(res.decision?.answer_label).toBe("Merge with the WARN");
  });

  it("throws an ApiError on a non-409 failure", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "boom" }), { status: 500 }));
    vi.stubGlobal("fetch", fetch);
    await expect(decisionsApi.answerDecision("a", { option_id: "merge" })).rejects.toMatchObject({ name: "ApiError", status: 500 });
  });

  it("cancels via POST with a reason", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ decision: decision("a", { state: "cancelled" }) })));
    vi.stubGlobal("fetch", fetch);
    const res = await decisionsApi.cancelDecision("a", "dismissed");
    expect(res.decision.state).toBe("cancelled");
    const [url, init] = fetch.mock.calls[0];
    expect(String(url)).toContain("/api/decisions/a/cancel");
    expect(JSON.parse(init.body as string)).toEqual({ reason: "dismissed" });
  });
});
