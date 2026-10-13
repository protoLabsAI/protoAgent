// useSteerQueue (#3862) — the mid-turn steer queue and its reconcile, extracted from
// ChatSessionSlot. Drives the hook through the same minimal createRoot/act renderHook as
// useAttachments.test.tsx, against the real chat store (the reconcile reads live store
// state) with the steering endpoints spied. Pins: queueing a steer / an interjection and
// their failure paths, the transcript-settle effect, ✕ dequeue (removed / consumed /
// failed), the own-stream turn-end reconcile (settle, re-send, the HITL hold), the idle
// effect's live-stream guard and its status → idle trigger, the server-interjection
// reconcile on mount (re-send vs the HITL hold) and on a live task-id change, Stop's
// clear, persistence, and per-render closures.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";

import { api } from "../lib/api";
import type { ChatMessage, HitlPayload, QueuedSteer } from "../lib/types";
import { chatStore } from "./chat-store";
import { loadSteers, saveSteers } from "./scratchState";
import { useSteerQueue, type UseSteerQueueOptions } from "./useSteerQueue";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type HookResult = ReturnType<typeof useSteerQueue>;

let container: HTMLElement;
let root: Root;
let result: { current: HookResult };
let Probe: (props: UseSteerQueueOptions) => null;

function renderHook(opts: UseSteerQueueOptions) {
  result = { current: undefined as unknown as HookResult };
  Probe = function Probe(props: UseSteerQueueOptions) {
    result.current = useSteerQueue(props);
    return null;
  };
  act(() => root.render(h(Probe, opts)));
  return result;
}

// Re-render the SAME mounted Probe with new options (state and refs survive).
function rerender(opts: UseSteerQueueOptions) {
  act(() => root.render(h(Probe, opts)));
}

// Let the mocked-API promise chains settle inside act.
async function flush() {
  await act(async () => {
    for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0));
  });
}

// A draft box standing in for the slot's useState: setDraft takes values or updaters.
let draftBox: { value: string };
const setDraft = vi.fn((next: string | ((d: string) => string)) => {
  draftBox.value = typeof next === "function" ? next(draftBox.value) : next;
});

const mkRun = () => vi.fn<(content: string) => Promise<void>>(async () => {});

let sessionId: string;
const messagesOf = () => chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.messages ?? [];

const baseOpts = (over: Partial<UseSteerQueueOptions> = {}): UseSteerQueueOptions => ({
  sessionId,
  session: { id: sessionId },
  messages: messagesOf(),
  status: "streaming",
  visible: false,
  serverTurnControl: null,
  serverTurnLabel: null,
  draft: draftBox.value,
  setDraft,
  onError: vi.fn(),
  runTurn: mkRun(),
  hitlRef: { current: null },
  abortRef: { current: null },
  recordSubmitted: vi.fn(),
  ...over,
});

let steerChat: MockInstance<typeof api.steerChat>;
let pendingSteer: MockInstance<typeof api.pendingSteer>;
let cancelSteer: MockInstance<typeof api.cancelSteer>;
let serverTurnInterject: MockInstance<typeof api.serverTurnInterject>;
let taskSteerState: MockInstance<typeof api.taskSteerState>;

beforeEach(() => {
  window.sessionStorage.clear();
  draftBox = { value: "" };
  setDraft.mockClear();
  // A fresh session with a transcript, so settles have somewhere to land.
  sessionId = chatStore.createSession().id;
  chatStore.updateMessages(sessionId, [
    { id: "u0", role: "user", content: "hello", createdAt: 1, status: "done" },
    { id: "a0", role: "assistant", content: "hi", createdAt: 2, status: "done" },
  ]);
  steerChat = vi.spyOn(api, "steerChat").mockResolvedValue({ ok: true, id: null, pending: 1 });
  pendingSteer = vi.spyOn(api, "pendingSteer").mockResolvedValue({ pending: [], drained: [] });
  cancelSteer = vi.spyOn(api, "cancelSteer").mockResolvedValue({ removed: true, pending: 0 });
  serverTurnInterject = vi.spyOn(api, "serverTurnInterject").mockResolvedValue({ ok: true, pending: 1 });
  taskSteerState = vi.spyOn(api, "taskSteerState").mockResolvedValue({ state: "", consumed: [] });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  chatStore.clearServerTurnControl(sessionId);
});

const ids = (q: QueuedSteer[]) => q.map((x) => x.id);

describe("useSteerQueue — queueing", () => {
  it("queues a steer optimistically, clears the draft, records history, and POSTs it", async () => {
    draftBox.value = "  go left  ";
    const recordSubmitted = vi.fn();
    const r = renderHook(baseOpts({ recordSubmitted }));
    await act(() => r.current.queueSteer());
    expect(recordSubmitted).toHaveBeenCalledWith("go left");
    expect(draftBox.value).toBe("");
    expect(r.current.steerQueue).toMatchObject([{ text: "go left" }]);
    expect(r.current.steerQueueRef.current).toBe(r.current.steerQueue);
    const id = r.current.steerQueue[0].id;
    expect(steerChat).toHaveBeenCalledWith(sessionId, id, "go left");
    // Persisted so a swap (S3) restores it.
    expect(ids(loadSteers(sessionId))).toEqual([id]);
  });

  it("does nothing with an empty draft or no session", async () => {
    const r = renderHook(baseOpts({ draft: "   " }));
    await act(() => r.current.queueSteer());
    rerender(baseOpts({ draft: "x", session: null }));
    await act(() => r.current.queueSteer());
    expect(steerChat).not.toHaveBeenCalled();
    expect(r.current.steerQueue).toEqual([]);
  });

  it("takes the steer back and reports when the POST fails", async () => {
    steerChat.mockRejectedValue(new Error("offline"));
    const onError = vi.fn();
    const r = renderHook(baseOpts({ draft: "go", onError }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(r.current.steerQueue).toEqual([]);
    expect(onError).toHaveBeenCalledWith("Couldn't queue message: offline");
  });

  it("queues an interjection tagged with the live server task, confirming it on ack", async () => {
    chatStore.setServerTurnControl({
      sessionId,
      taskId: "t-live",
      origin: "scheduler",
      trigger: "x",
      controllable: true,
      operatorControllable: true,
    });
    const control = { taskId: "t-live" };
    const r = renderHook(baseOpts({ draft: "also this", serverTurnControl: control }));
    await act(() => r.current.queueServerInterjection());
    await flush();
    const id = r.current.steerQueue[0].id;
    expect(serverTurnInterject).toHaveBeenCalledWith(sessionId, "t-live", id, "also this");
    expect(r.current.steerQueue).toEqual([{ id, text: "also this", serverTaskId: "t-live" }]);
  });

  it("hands a REFUSED interjection back into the composer (appended), and drops the stale control", async () => {
    chatStore.setServerTurnControl({
      sessionId,
      taskId: "t-gone",
      origin: "scheduler",
      trigger: "x",
      controllable: true,
      operatorControllable: true,
    });
    serverTurnInterject.mockResolvedValue({ ok: false, pending: 0, reason: "not_live" });
    const onError = vi.fn();
    const r = renderHook(baseOpts({ draft: "late words", serverTurnControl: { taskId: "t-gone" }, onError }));
    // The operator types more while the POST is out.
    let pending!: Promise<void>;
    act(() => {
      pending = r.current.queueServerInterjection();
    });
    draftBox.value = "new draft";
    await act(() => pending);
    await flush();
    expect(r.current.steerQueue).toEqual([]);
    expect(draftBox.value).toBe("new draft\n\nlate words");
    expect(onError).toHaveBeenCalledWith(
      "That server task had already finished, so your message wasn't sent — its text is in the composer.",
    );
    expect(chatStore.getSnapshot().serverTurnControls[sessionId]).toBeUndefined();
  });
});

describe("useSteerQueue — settle + dequeue", () => {
  it("retires a queued bubble once its id appears in the transcript", async () => {
    const r = renderHook(baseOpts({ draft: "fold me" }));
    await act(() => r.current.queueSteer());
    const id = r.current.steerQueue[0].id;
    const withIt: ChatMessage[] = [...messagesOf(), { id, role: "user", content: "fold me", createdAt: 3, status: "done" }];
    rerender(baseOpts({ messages: withIt }));
    expect(r.current.steerQueue).toEqual([]);
  });

  it("settleConsumed moves consumed steers out of the queue and into the transcript", async () => {
    const r = renderHook(baseOpts({ draft: "fold me" }));
    await act(() => r.current.queueSteer());
    const id = r.current.steerQueue[0].id;
    act(() => r.current.settleConsumed([{ id, text: "fold me" }]));
    expect(r.current.steerQueue).toEqual([]);
    expect(messagesOf().some((m) => m.id === id && m.content === "fold me")).toBe(true);
  });

  it("✕ drops a steer the server still held", async () => {
    const r = renderHook(baseOpts({ draft: "cancel me" }));
    await act(() => r.current.queueSteer());
    const id = r.current.steerQueue[0].id;
    let outcome = "";
    await act(async () => {
      outcome = await r.current.dequeueSteer(id);
    });
    expect(outcome).toBe("removed");
    expect(cancelSteer).toHaveBeenCalledWith(sessionId, id);
    expect(r.current.steerQueue).toEqual([]);
  });

  it("restores a steer the agent had already drained (removed:false), reporting consumed", async () => {
    cancelSteer.mockResolvedValue({ removed: false, pending: 0 });
    const r = renderHook(baseOpts({ draft: "too late" }));
    await act(() => r.current.queueSteer());
    const id = r.current.steerQueue[0].id;
    let outcome = "";
    await act(async () => {
      outcome = await r.current.dequeueSteer(id);
    });
    expect(outcome).toBe("consumed");
    expect(ids(r.current.steerQueue)).toEqual([id]);
  });

  it("restores the bubble and reports when the cancel can't reach the server", async () => {
    cancelSteer.mockRejectedValue(new Error("down"));
    const onError = vi.fn();
    const r = renderHook(baseOpts({ draft: "keep me", onError }));
    await act(() => r.current.queueSteer());
    const id = r.current.steerQueue[0].id;
    await act(() => r.current.cancelSteer(id));
    expect(ids(r.current.steerQueue)).toEqual([id]);
    expect(onError).toHaveBeenCalledWith("Couldn't cancel message: down");
  });
});

describe("useSteerQueue — own-stream turn-end reconcile", () => {
  async function queueTwo(r: { current: HookResult }) {
    rerender(baseOpts({ draft: "one" }));
    await act(() => r.current.queueSteer());
    rerender(baseOpts({ draft: "two" }));
    await act(() => r.current.queueSteer());
    return ids(r.current.steerQueue);
  }

  it("settles the consumed steers and re-sends the never-seen ones as a fresh turn", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    const [a, b] = await queueTwo(r);
    rerender(baseOpts({ runTurn }));
    pendingSteer.mockResolvedValue({ pending: [{ id: b, text: "two" }] });
    await act(() => r.current.reconcileSteer());
    expect(messagesOf().some((m) => m.id === a)).toBe(true);
    expect(runTurn).toHaveBeenCalledWith("two");
    expect(r.current.steerQueue).toEqual([]);
  });

  it("keeps unread steers QUEUED while a HITL form is parked (never re-sends before the answer)", async () => {
    const runTurn = mkRun();
    const hitlRef = { current: { kind: "form" } as HitlPayload };
    const r = renderHook(baseOpts({ runTurn, hitlRef }));
    const [, b] = await queueTwo(r);
    rerender(baseOpts({ runTurn, hitlRef }));
    pendingSteer.mockResolvedValue({ pending: [{ id: b, text: "two" }] });
    await act(() => r.current.reconcileSteer());
    expect(runTurn).not.toHaveBeenCalled();
    expect(ids(r.current.steerQueue)).toEqual([b]);
  });

  it("leaves the queue alone when the pending read fails", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    const both = await queueTwo(r);
    rerender(baseOpts({ runTurn }));
    pendingSteer.mockRejectedValue(new Error("x"));
    await act(() => r.current.reconcileSteer());
    expect(ids(r.current.steerQueue)).toEqual(both);
    expect(runTurn).not.toHaveBeenCalled();
  });

  it("the idle effect reconciles a queued steer when no local stream owns the session", async () => {
    saveSteers(sessionId, [{ id: "s1", text: "left over" }]);
    const runTurn = mkRun();
    pendingSteer.mockResolvedValue({ pending: [{ id: "s1", text: "left over" }] });
    renderHook(baseOpts({ runTurn, status: "idle" }));
    await flush();
    expect(runTurn).toHaveBeenCalledWith("left over");
  });

  it("the idle effect re-runs when the status goes streaming → idle (a reattached turn ending)", async () => {
    saveSteers(sessionId, [{ id: "s1", text: "left over" }]);
    const runTurn = mkRun();
    pendingSteer.mockResolvedValue({ pending: [{ id: "s1", text: "left over" }] });
    renderHook(baseOpts({ runTurn, status: "streaming" }));
    await flush();
    expect(pendingSteer).not.toHaveBeenCalled();
    // The turn this slot did not run ends: same session, no new mount — only the status moves.
    rerender(baseOpts({ runTurn, status: "idle" }));
    await flush();
    expect(runTurn).toHaveBeenCalledWith("left over");
  });

  it("the idle effect stays out of a stream this slot owns (abortRef set)", async () => {
    saveSteers(sessionId, [{ id: "s1", text: "left over" }]);
    const runTurn = mkRun();
    pendingSteer.mockResolvedValue({ pending: [{ id: "s1", text: "left over" }] });
    const r = renderHook(baseOpts({ runTurn, status: "idle", abortRef: { current: new AbortController() } }));
    await flush();
    expect(pendingSteer).not.toHaveBeenCalled();
    expect(runTurn).not.toHaveBeenCalled();
    expect(ids(r.current.steerQueue)).toEqual(["s1"]);
  });
});

describe("useSteerQueue — server-interjection reconcile", () => {
  // An interjection queued (and acknowledged) for a server turn that has since ENDED, still
  // in the server's queue, with nothing else live to drain it: the reconcile dequeues it
  // and sends it as the operator's next message.
  function seedStranded() {
    saveSteers(sessionId, [{ id: "i1", text: "stranded", serverTaskId: "t-old" }]);
    pendingSteer.mockResolvedValue({ pending: [{ id: "i1", text: "stranded" }], drained: [] });
    taskSteerState.mockResolvedValue({ state: "completed", consumed: [] });
  }

  it("re-sends a stranded interjection as a fresh turn on mount", async () => {
    seedStranded();
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn, status: "idle" }));
    await flush();
    expect(taskSteerState).toHaveBeenCalledWith("t-old");
    expect(cancelSteer).toHaveBeenCalledWith(sessionId, "i1");
    expect(runTurn).toHaveBeenCalledWith("stranded");
    expect(r.current.steerQueue).toEqual([]);
  });

  it("holds it while a HITL form is parked (the answer drains the queue)", async () => {
    seedStranded();
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn, status: "idle", hitlRef: { current: { kind: "form" } } }));
    await flush();
    expect(runTurn).not.toHaveBeenCalled();
    expect(cancelSteer).not.toHaveBeenCalled();
    expect(ids(r.current.steerQueue)).toEqual(["i1"]);
  });

  it("settles one the turn's durable history says it consumed", async () => {
    saveSteers(sessionId, [{ id: "i2", text: "read it", serverTaskId: "t-old" }]);
    taskSteerState.mockResolvedValue({ state: "completed", consumed: ["i2"] });
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn, status: "idle" }));
    await flush();
    expect(runTurn).not.toHaveBeenCalled();
    expect(r.current.steerQueue).toEqual([]);
    expect(messagesOf().some((m) => m.id === "i2" && m.content === "read it")).toBe(true);
  });

  it("re-runs when the live control's task id changes while the turn label stays the same", async () => {
    chatStore.setServerTurnControl({
      sessionId,
      taskId: "t1",
      origin: "scheduler",
      trigger: "x",
      controllable: true,
      operatorControllable: true,
    });
    saveSteers(sessionId, [{ id: "i1", text: "for t1", serverTaskId: "t1" }]);
    pendingSteer.mockResolvedValue({ pending: [{ id: "i1", text: "for t1" }], drained: [] });
    taskSteerState.mockResolvedValue({ state: "canceled", consumed: [] });
    const runTurn = mkRun();
    const label = "Scheduled: nightly digest";
    renderHook(baseOpts({ runTurn, status: "idle", serverTurnControl: { taskId: "t1" }, serverTurnLabel: label }));
    await flush();
    // Its turn is the live one: nothing stale, so nothing asked.
    expect(pendingSteer).not.toHaveBeenCalled();
    // stop() clears the control while useServerTurn still reports the label.
    chatStore.clearServerTurnControl(sessionId);
    rerender(baseOpts({ runTurn, status: "idle", serverTurnControl: null, serverTurnLabel: label }));
    await flush();
    expect(pendingSteer).toHaveBeenCalledWith(sessionId);
    expect(runTurn).toHaveBeenCalledWith("for t1");
  });

  // An interjection whose server turn still reads "working" is kept and re-checked on the
  // backoff ladder; a focus / visibility return re-checks NOW — for a visible slot only.
  function seedWaiting() {
    saveSteers(sessionId, [{ id: "i3", text: "waiting", serverTaskId: "t-old" }]);
    pendingSteer.mockResolvedValue({ pending: [{ id: "i3", text: "waiting" }], drained: [] });
    taskSteerState.mockResolvedValue({ state: "working", consumed: [] });
  }

  it("re-checks at once when a visible slot's tab regains focus", async () => {
    seedWaiting();
    renderHook(baseOpts({ visible: true }));
    await flush();
    expect(pendingSteer).toHaveBeenCalledTimes(1); // the mount reconcile
    act(() => void window.dispatchEvent(new Event("focus")));
    await flush();
    expect(pendingSteer).toHaveBeenCalledTimes(2);
  });

  it("a hidden slot does not listen for focus", async () => {
    seedWaiting();
    renderHook(baseOpts({ visible: false }));
    await flush();
    act(() => void window.dispatchEvent(new Event("focus")));
    await flush();
    expect(pendingSteer).toHaveBeenCalledTimes(1);
  });
});

describe("useSteerQueue — Stop + persistence + closures", () => {
  it("clearQueueOnStop empties the queue now and hands un-read words back to the composer", async () => {
    const onError = vi.fn();
    const r = renderHook(baseOpts({ draft: "never sent", onError }));
    await act(() => r.current.queueSteer());
    act(() => r.current.clearQueueOnStop());
    expect(r.current.steerQueue).toEqual([]);
    await flush();
    expect(draftBox.value).toBe("never sent");
    expect(onError).toHaveBeenCalledWith("That queued message was never sent — its text is in the composer.");
  });

  it("clearQueueOnStop settles a message the agent had already read instead", async () => {
    cancelSteer.mockResolvedValue({ removed: false, pending: 0 });
    const r = renderHook(baseOpts({ draft: "was read" }));
    await act(() => r.current.queueSteer());
    const id = r.current.steerQueue[0].id;
    act(() => r.current.clearQueueOnStop());
    await flush();
    expect(draftBox.value).toBe("");
    expect(messagesOf().some((m) => m.id === id)).toBe(true);
  });

  it("restores a persisted queue on mount", () => {
    saveSteers(sessionId, [{ id: "p1", text: "from before" }]);
    const r = renderHook(baseOpts());
    expect(r.current.steerQueue).toEqual([{ id: "p1", text: "from before" }]);
    expect(r.current.steerQueueRef.current).toEqual([{ id: "p1", text: "from before" }]);
  });

  it("a reconcile from a later render re-sends through THAT render's runTurn", async () => {
    const first = mkRun();
    const second = mkRun();
    saveSteers(sessionId, [{ id: "s9", text: "again" }]);
    const r = renderHook(baseOpts({ runTurn: first }));
    rerender(baseOpts({ runTurn: second }));
    pendingSteer.mockResolvedValue({ pending: [{ id: "s9", text: "again" }] });
    await act(() => r.current.reconcileSteer());
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith("again");
  });
});
