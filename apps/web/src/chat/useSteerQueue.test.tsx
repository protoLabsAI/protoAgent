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
import { __resetForTest as resetConversations, subscribeForTest as subscribeConversations } from "./conversations-store";
import {
  PRIVATE_STEER_TEXT_ONLY,
  PRIVATE_STEER_UNAVAILABLE,
  PRIVATE_STEER_WAITING_ON_PEER,
  PRIVATE_STEER_DELIVERY_FAILED,
} from "./privateSteer";
import { noteParkedTurnResumed, reattachOrReconcile } from "./reattach";
import { noteTurnFinished } from "./server-turn-store";
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

// The per-message settings every steer/interjection POST now carries (bd-0ad5v review): a
// turn the server starts for the message must run under the tab's picks, not the defaults.
// A fresh session has no model/bypass pick and reasons at the default level.
const SETTINGS = { reasoningEffort: "medium" };

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
    expect(steerChat).toHaveBeenCalledWith(sessionId, id, "go left", undefined, undefined, SETTINGS);
    // Persisted so a swap (S3) restores it.
    expect(ids(loadSteers(sessionId))).toEqual([id]);
  });

  it("carries the tab's chosen model/effort/bypass, read live (bd-0ad5v review)", async () => {
    // A turn the server starts for this message (idle session, or the turn-end restart)
    // must run under the tab's settings — the old re-send path carried them via runTurn.
    chatStore.setSessionModel(sessionId, "sonnet-x");
    chatStore.setSessionReasoningEffort(sessionId, "high");
    chatStore.setSessionBypassPermissions(sessionId, true);
    const r = renderHook(baseOpts({ draft: "go" }));
    await act(() => r.current.queueSteer());
    const id = r.current.steerQueue[0].id;
    expect(steerChat).toHaveBeenCalledWith(sessionId, id, "go", undefined, undefined, {
      model: "sonnet-x",
      reasoningEffort: "high",
      bypassPermissions: true,
    });
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
    expect(serverTurnInterject).toHaveBeenCalledWith(sessionId, "t-live", id, "also this", SETTINGS);
    expect(r.current.steerQueue).toEqual([{ id, text: "also this", serverTaskId: "t-live" }]);
  });

  it("finalizes the stale bubble and attaches to the task a started_turn reply names (bd-0ad5v)", async () => {
    // The server had NO live turn: the 'streaming' this console believed in was stale, and
    // the steer ran as a fresh turn instead of queueing. The bubble retires, the zombie
    // stream is released, and the new task streams in via the slot's reattach effect.
    chatStore.updateMessages(sessionId, [
      { id: "u0", role: "user", content: "hello", createdAt: 1, status: "done" },
      { id: "a0", role: "assistant", content: "…", createdAt: 2, status: "streaming" },
    ]);
    steerChat.mockResolvedValue({ ok: true, id: null, pending: 0, started_turn: "task-9" });
    const abort = vi.fn();
    const abortRef = { current: { abort } as unknown as AbortController | null };
    const r = renderHook(baseOpts({ draft: "are you there?", abortRef }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(r.current.steerQueue).toEqual([]); // retired — the turn's own user message replaces it
    expect(abort).toHaveBeenCalled();
    expect(abortRef.current).toBeNull();
    const msgs = messagesOf();
    expect(msgs.map((m) => m.status)).toEqual(["done", "done", "done", "streaming"]);
    expect(msgs[2]).toMatchObject({ role: "user", content: "are you there?" });
    expect(msgs[3]).toMatchObject({ role: "assistant", taskId: "task-9", status: "streaming" });
    expect(chatStore.getSnapshot().sessionStatusMap[sessionId]).toBe("streaming");
  });

  it.each(["before", "after"] as const)("keeps the resumed decision's bubble when started_turn arrives %s completion", async (replyTiming) => {
    // #5467: the steer POST and the resumed task's stream can finish in either order.
    const taskId = "decision-task";
    chatStore.updateMessages(sessionId, [{
      id: "approval", role: "assistant", content: "", status: "streaming",
      taskId, decisionId: "decision-1", paused: true,
      toolCalls: [{ id: "d1", name: "request_decision", status: "running", paused: true }],
    }]);
    let finish!: () => void;
    vi.spyOn(api, "resumeTask").mockImplementation(async (_task, _session, handlers) => {
      await new Promise<void>((resolve) => { finish = resolve; });
      handlers?.onText?.("All done.", false);
    });
    vi.spyOn(api, "getTask").mockResolvedValue({ state: "completed", text: "All done." });
    expect(noteParkedTurnResumed(sessionId, taskId)).toBe(true);
    const cancel = reattachOrReconcile(sessionId);
    let reply!: (value: Awaited<ReturnType<typeof api.steerChat>>) => void;
    steerChat.mockImplementation(() => new Promise((resolve) => { reply = resolve; }));
    const r = renderHook(baseOpts({ draft: "Approved" }));
    let queued!: Promise<void>;
    act(() => { queued = r.current.queueSteer(); });
    const acknowledge = async () => {
      reply({ ok: true, id: null, pending: 0, started_turn: taskId });
      await act(() => queued);
    };
    try {
      if (replyTiming === "before") await acknowledge();
      noteTurnFinished(sessionId, taskId);
      finish();
      await flush();
      if (replyTiming === "after") await acknowledge();
      expect(messagesOf().filter((m) => m.role === "assistant" && m.status === "streaming" && !m.content)).toEqual([]);
      expect(messagesOf().filter((m) => m.role === "assistant")).toMatchObject([
        { id: "approval", taskId, decisionId: "decision-1", status: "done", content: "All done." },
      ]);
      expect(messagesOf().filter((m) => m.role === "user")).toMatchObject([{ content: "Approved" }]);
      // The operator's message reads ABOVE the reply it caused, whichever finished first.
      const order = messagesOf().map((m) => (m.role === "user" ? m.content : m.id));
      expect(order.indexOf("Approved")).toBeGreaterThanOrEqual(0);
      expect(order.indexOf("Approved")).toBeLessThan(order.indexOf("approval"));
      expect(r.current.steerQueue).toEqual([]);
      expect(chatStore.getSnapshot().sessionStatusMap[sessionId]).toBe("idle");
    } finally {
      cancel?.();
    }
  });

  it.each(["streaming", "done"] as const)(
    "lands a steer that started task T between the prior turn and T's %s bubble",
    async (status) => {
      // The reattach / live-sync path already rendered T's bubble when the POST answers.
      chatStore.updateMessages(sessionId, [
        { id: "u0", role: "user", content: "hello", createdAt: 1, status: "done" },
        { id: "a0", role: "assistant", content: "hi", createdAt: 2, status: "done", taskId: "task-prior" },
        { id: "aT", role: "assistant", content: "I saved it.", createdAt: 3, status, taskId: "task-T" },
      ]);
      steerChat.mockResolvedValue({ ok: true, id: null, pending: 0, started_turn: "task-T" });
      const abort = vi.fn();
      const r = renderHook(baseOpts({ draft: "at knapsack", abortRef: { current: { abort } as unknown as AbortController } }));
      await act(() => r.current.queueSteer());
      await flush();
      const steerId = steerChat.mock.calls[0][1];
      expect(messagesOf().map((m) => m.id)).toEqual(["u0", "a0", steerId, "aT"]);
      expect(messagesOf()[2]).toMatchObject({ role: "user", content: "at knapsack", status: "done" });
      // T's bubble keeps its producer and state: no second placeholder, nothing reopened.
      expect(messagesOf()[3]).toMatchObject({ id: "aT", content: "I saved it.", status, taskId: "task-T" });
      expect(abort).not.toHaveBeenCalled();
      expect(r.current.steerQueue).toEqual([]);
    },
  );

  it("lands below an earlier steer inside a split task, and attaching the same id twice adds no duplicate", async () => {
    chatStore.updateMessages(sessionId, [
      { id: "u0", role: "user", content: "hello", createdAt: 1, status: "done" },
      { id: "a0", role: "assistant", content: "hi", createdAt: 2, status: "done", taskId: "task-prior" },
      { id: "frozen", role: "assistant", content: "Working.", createdAt: 3, status: "done", taskId: "task-T", splitOf: "aT" },
      { id: "s0", role: "user", content: "earlier steer", createdAt: 4, status: "done" },
      { id: "aT", role: "assistant", content: "Done.", createdAt: 5, status: "done", taskId: "task-T" },
    ]);
    steerChat.mockResolvedValue({ ok: true, id: null, pending: 0, started_turn: "task-T" });
    const r = renderHook(baseOpts({ draft: "at knapsack", status: "idle" }));
    await act(() => r.current.queueSteer());
    await flush();
    const steerId = steerChat.mock.calls[0][1];
    // Sent after "earlier steer", so below it — and still above the reply that follows.
    const placed = ["u0", "a0", "frozen", "s0", steerId, "aT"];
    expect(messagesOf().map((m) => m.id)).toEqual(placed);
    // The turn-end reconcile can name the same message for the same task again.
    saveSteers(sessionId, [{ id: steerId, text: "at knapsack" }]);
    act(() => root.unmount());
    root = createRoot(container);
    pendingSteer.mockResolvedValue({ pending: [], drained: [], started: { [steerId]: "task-T" } });
    const again = renderHook(baseOpts({ status: "idle" }));
    await flush();
    expect(again.current.steerQueue).toEqual([]);
    expect(messagesOf().map((m) => m.id)).toEqual(placed);
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

describe("useSteerQueue — steer attachments (#4645)", () => {
  const IMG = { b64: "aGVsbG8=", mime: "image/png", name: "shot.png" };

  it("carries the tray's native images with a steer and clears the tray once accepted", async () => {
    const clearTrayImages = vi.fn();
    const r = renderHook(baseOpts({ draft: "look here", trayImages: () => [IMG], clearTrayImages }));
    await act(() => r.current.queueSteer());
    await flush();
    const id = r.current.steerQueue[0].id;
    expect(steerChat).toHaveBeenCalledWith(sessionId, id, "look here", [IMG], undefined, SETTINGS);
    // Cleared ONLY after the POST resolved, so a failure would have kept them.
    expect(clearTrayImages).toHaveBeenCalledTimes(1);
  });


  it("preserves images across scratch reload and late re-send", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ trayImages: () => [IMG], runTurn }));
    await act(() => r.current.queueSteer());
    const item = r.current.steerQueue[0];
    expect(loadSteers(sessionId)).toEqual([{ ...item, images: [IMG] }]);
    pendingSteer.mockResolvedValue({ pending: [{ id: item.id, text: "" }] });
    await act(() => r.current.reconcileSteer());
    expect(runTurn).toHaveBeenCalledWith("", { images: [IMG] });
  });

  it.each(["edit", "stop"] as const)("returns accepted images to the tray on %s", async (action) => {
    const restoreTrayImages = vi.fn();
    const r = renderHook(baseOpts({ trayImages: () => [IMG], restoreTrayImages }));
    await act(() => r.current.queueSteer());
    const id = r.current.steerQueue[0].id;
    await act(async () => {
      if (action === "edit") await r.current.dequeueSteer(id, true);
      else r.current.clearQueueOnStop();
    });
    await flush();
    expect(restoreTrayImages).toHaveBeenCalledWith([IMG]);
    expect(r.current.steerQueue).toEqual([]);
  });

  it("does not restore images when recall loses the race to consumption", async () => {
    const restoreTrayImages = vi.fn();
    const r = renderHook(baseOpts({ trayImages: () => [IMG], restoreTrayImages }));
    await act(() => r.current.queueSteer());
    cancelSteer.mockResolvedValue({ removed: false, pending: 0 });
    await act(() => r.current.dequeueSteer(r.current.steerQueue[0].id, true));
    expect(restoreTrayImages).not.toHaveBeenCalled();
    expect(r.current.steerQueue[0].images).toEqual([IMG]);
  });

  it("asserts incognito on an image steer", async () => {
    const r = renderHook(baseOpts({ session: { id: sessionId, incognito: true }, trayImages: () => [IMG] }));
    await act(() => r.current.queueSteer());
    expect(steerChat).toHaveBeenCalledWith(sessionId, r.current.steerQueue[0].id, "", [IMG], true, SETTINGS);
  });

  it("waits for acceptance before clearing images", async () => {
    let accept!: () => void;
    steerChat.mockImplementation(() => new Promise((resolve) => {
      accept = () => resolve({ ok: true, id: null, pending: 1 });
    }));
    const clearTrayImages = vi.fn();
    const r = renderHook(baseOpts({ trayImages: () => [IMG], clearTrayImages }));
    let request!: Promise<void>;
    act(() => { request = r.current.queueSteer(); });
    expect(clearTrayImages).not.toHaveBeenCalled();
    await act(async () => { accept(); await request; });
    expect(clearTrayImages).toHaveBeenCalledOnce();
  });

  it("queues an image-only steer with an empty draft (the image is the message)", async () => {
    const clearTrayImages = vi.fn();
    const r = renderHook(baseOpts({ draft: "   ", trayImages: () => [IMG], clearTrayImages }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(r.current.steerQueue).toMatchObject([{ text: "" }]);
    const id = r.current.steerQueue[0].id;
    expect(steerChat).toHaveBeenCalledWith(sessionId, id, "", [IMG], undefined, SETTINGS);
    expect(clearTrayImages).toHaveBeenCalledTimes(1);
  });

  it("keeps the tray's images (and reports) when the steer POST fails", async () => {
    steerChat.mockRejectedValue(new Error("offline"));
    const clearTrayImages = vi.fn();
    const onError = vi.fn();
    const r = renderHook(baseOpts({ draft: "look", trayImages: () => [IMG], clearTrayImages, onError }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(clearTrayImages).not.toHaveBeenCalled();
    expect(r.current.steerQueue).toEqual([]);
    expect(onError).toHaveBeenCalledWith("Couldn't queue message: offline");
  });

  it("a text-only steer passes no images and never touches the tray", async () => {
    const clearTrayImages = vi.fn();
    const r = renderHook(baseOpts({ draft: "just text", trayImages: () => [], clearTrayImages }));
    await act(() => r.current.queueSteer());
    await flush();
    const id = r.current.steerQueue[0].id;
    expect(steerChat).toHaveBeenCalledWith(sessionId, id, "just text", undefined, undefined, SETTINGS);
    expect(clearTrayImages).not.toHaveBeenCalled();
  });

  it("a server interjection stays text-only — it never reads or clears the tray", async () => {
    chatStore.setServerTurnControl({
      sessionId,
      taskId: "t-live",
      origin: "scheduler",
      trigger: "x",
      controllable: true,
      operatorControllable: true,
    });
    const trayImages = vi.fn(() => [IMG]);
    const clearTrayImages = vi.fn();
    const r = renderHook(
      baseOpts({ draft: "also this", serverTurnControl: { taskId: "t-live" }, trayImages, clearTrayImages }),
    );
    await act(() => r.current.queueServerInterjection());
    await flush();
    const id = r.current.steerQueue[0].id;
    expect(serverTurnInterject).toHaveBeenCalledWith(sessionId, "t-live", id, "also this", SETTINGS);
    expect(trayImages).not.toHaveBeenCalled();
    expect(clearTrayImages).not.toHaveBeenCalled();
  });

  it("a HITL-held message (queued via queueSteer) stays text-only and leaves the tray alone", async () => {
    const trayImages = vi.fn(() => [IMG]);
    const clearTrayImages = vi.fn();
    const hitlRef = { current: { kind: "form" } as HitlPayload };
    const trayContext = vi.fn(() => PIPED);
    const clearTrayContext = vi.fn();
    const r = renderHook(baseOpts({ draft: "answer later", hitlRef, trayImages, clearTrayImages, trayContext, clearTrayContext }));
    await act(() => r.current.queueSteer());
    await flush();
    const id = r.current.steerQueue[0].id;
    expect(steerChat).toHaveBeenCalledWith(sessionId, id, "answer later", undefined, undefined, SETTINGS);
    expect(trayContext).not.toHaveBeenCalled();
    expect(clearTrayContext).not.toHaveBeenCalled();
    expect(trayImages).not.toHaveBeenCalled();
    expect(clearTrayImages).not.toHaveBeenCalled();
  });
});

// #5775: pipeline context rides as text, while display and recall stay human-readable.
const PIPED = { contexts: ["<context>large pasted content</context>"], names: ["Pasted text.txt"] };

describe("pipeline attachments", () => {
  it.each(["use this", ""])("sends context with draft %j and clears only after acceptance", async (draft) => {
    let accept!: () => void;
    steerChat.mockImplementation(() => new Promise((resolve) => {
      accept = () => resolve({ ok: true, id: null, pending: 1 });
    }));
    const clearTrayContext = vi.fn();
    const r = renderHook(baseOpts({ draft, trayContext: () => PIPED, clearTrayContext }));
    let request!: Promise<void>;
    act(() => { request = r.current.queueSteer(); });
    const item = r.current.steerQueue[0];
    const display = [draft, "Attached: Pasted text.txt"].filter(Boolean).join("\n\n");
    expect(item.text).toBe(display);
    expect(item.draftText).toBe(draft);
    expect(steerChat.mock.calls[0][2]).toBe([...PIPED.contexts, draft].join("\n\n").trim());
    expect(clearTrayContext).not.toHaveBeenCalled();
    await act(async () => { accept(); await request; });
    expect(clearTrayContext).toHaveBeenCalledOnce();
    expect(loadSteers(sessionId)[0]).toEqual(item);
    // Turn-end retry retains context while its user bubble remains readable.
    pendingSteer.mockResolvedValue({ pending: [{ id: item.id, text: item.sendAs! }], drained: [] });
    const runTurn = mkRun();
    rerender(baseOpts({ runTurn }));
    await act(() => r.current.reconcileSteer());
    expect(runTurn).toHaveBeenCalledWith(display, { sendAs: item.sendAs, display });
  });

  it("hands back only typed words after accepted attachments were cleared", async () => {
    const r = renderHook(baseOpts({ draft: "use this", trayContext: () => PIPED }));
    await act(() => r.current.queueSteer());
    act(() => r.current.clearQueueOnStop());
    await flush();
    expect(draftBox.value).toBe("use this");
    expect(r.current.steerQueue).toEqual([]);
  });

  it("keeps pipeline attachments when a steer fails", async () => {
    steerChat.mockRejectedValue(new Error("offline"));
    const clearTrayContext = vi.fn();
    const r = renderHook(baseOpts({ draft: "use this", trayContext: () => PIPED, clearTrayContext }));
    await act(() => r.current.queueSteer());
    expect(clearTrayContext).not.toHaveBeenCalled();
    expect(r.current.steerQueue).toEqual([]);
  });

  it.each(["ok", "duplicate", "refused", "error"] as const)(
    "server interjection %s carries context and clears only on acknowledgement",
    async (outcome) => {
      chatStore.setServerTurnControl({
        sessionId, taskId: "t-live", origin: "scheduler", trigger: "x",
        controllable: true, operatorControllable: true,
      });
      if (outcome === "error") serverTurnInterject.mockRejectedValue(new Error("offline"));
      else serverTurnInterject.mockResolvedValue(
        outcome === "ok" ? { ok: true, pending: 1 }
          : { ok: false, pending: 0, reason: outcome === "duplicate" ? "duplicate" : "not_live" },
      );
      const clearTrayContext = vi.fn();
      const r = renderHook(baseOpts({
        draft: "use this", serverTurnControl: { taskId: "t-live" },
        trayContext: () => PIPED, clearTrayContext,
      }));
      await act(() => r.current.queueServerInterjection());
      expect(serverTurnInterject.mock.calls[0][3]).toBe(PIPED.contexts[0] + "\n\nuse this");
      expect(clearTrayContext).toHaveBeenCalledTimes(outcome === "ok" || outcome === "duplicate" ? 1 : 0);
      if (outcome === "refused") expect(draftBox.value).toBe("use this");
      else expect(r.current.steerQueue[0].text).toBe("use this\n\nAttached: Pasted text.txt");
    },
  );
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
    // Dequeued BEFORE the re-send: left on the server, the turn-end restart (or the fresh
    // turn itself) would fold the same words in again.
    expect(cancelSteer).toHaveBeenCalledWith(sessionId, b);
    expect(runTurn).toHaveBeenCalledWith("two");
    expect(r.current.steerQueue).toEqual([]);
  });

  it("does not re-send a steer whose dequeue lost the race — the next pass reads what took it", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    rerender(baseOpts({ draft: "one", runTurn }));
    await act(() => r.current.queueSteer());
    const [a] = ids(r.current.steerQueue);
    rerender(baseOpts({ runTurn }));
    pendingSteer.mockResolvedValue({ pending: [{ id: a, text: "one" }] });
    cancelSteer.mockResolvedValue({ removed: false, pending: 0 });
    await act(() => r.current.reconcileSteer());
    expect(runTurn).not.toHaveBeenCalled();
    expect(ids(r.current.steerQueue)).toEqual([a]);
  });

  it("attaches to the restarted turn instead of settling when the server ran leftovers as a fresh turn (bd-0ad5v)", async () => {
    // The blocking review case: the turn ended with both steers undrained, and the server
    // popped them into a loopback turn of their own. They are gone from `pending` and in
    // `drained` — which used to read as "consumed by the turn that just ended" — but
    // `started` names the fresh task: the reply comes from THERE, so the bubbles become
    // the turn's own user messages and a streaming assistant bubble carries its task id
    // for the slot's reattach effect to subscribe through.
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    const [a, b] = await queueTwo(r);
    rerender(baseOpts({ runTurn }));
    pendingSteer.mockResolvedValue({ pending: [], drained: [a, b], started: { [a]: "task-r", [b]: "task-r" } });
    await act(() => r.current.reconcileSteer());
    expect(runTurn).not.toHaveBeenCalled(); // never re-sent — the restart already carries the words
    expect(r.current.steerQueue).toEqual([]);
    const msgs = messagesOf();
    expect(msgs.filter((m) => m.role === "user").map((m) => m.id)).toEqual(["u0", a, b]);
    expect(msgs[msgs.length - 1]).toMatchObject({ role: "assistant", taskId: "task-r", status: "streaming" });
    expect(chatStore.getSnapshot().sessionStatusMap[sessionId]).toBe("streaming");
  });

  it("keeps a steer queued while its restart hasn't named a task yet (started: '')", async () => {
    const runTurn = mkRun();
    const r = renderHook(baseOpts({ runTurn }));
    rerender(baseOpts({ draft: "one", runTurn }));
    await act(() => r.current.queueSteer());
    const [a] = ids(r.current.steerQueue);
    rerender(baseOpts({ runTurn }));
    pendingSteer.mockResolvedValue({ pending: [], drained: [a], started: { [a]: "" } });
    await act(() => r.current.reconcileSteer());
    expect(ids(r.current.steerQueue)).toEqual([a]); // neither settled nor re-sent — asked again
    expect(runTurn).not.toHaveBeenCalled();
    expect(messagesOf().some((m) => m.id === a)).toBe(false);
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

// #5400: a conversation a peer started (a row in the Conversations store — "From others") is
// always steered PRIVATE. These drive the real api.steerChat down to fetch, so the assertions
// are on the body the server actually receives.
describe("useSteerQueue — From others conversations are steered private (#5400)", () => {
  const IMG = { b64: "aGVsbG8=", mime: "image/png", name: "shot.png" };
  let bodies: Array<Record<string, unknown>>;
  let unsubscribe: () => void;

  // Answer the steer POST from `reply`; everything else the store's hydration touches is
  // spied at the api layer.
  function stubSteerFetch(reply: () => Response) {
    bodies = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        bodies.push(JSON.parse(String(init?.body)));
        return reply();
      }),
    );
  }
  const accepted = () =>
    new Response(JSON.stringify({ ok: true, id: null, pending: 1 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  const refused = (status: number, detail: string) => () =>
    new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json" } });

  /** List `id` under From others: hydrate the real Conversations store with a row for it. */
  async function listFromOthers(id: string) {
    vi.spyOn(api, "listConversations").mockResolvedValue({
      conversations: [
        {
          session_id: id,
          title: "Peer thread",
          origin_label: "From another agent",
          group: "automated",
          last_updated: new Date().toISOString(),
          turn_count: 1,
          active: true,
          last_state: "working",
        },
      ],
    } as Awaited<ReturnType<typeof api.listConversations>>);
    unsubscribe = subscribeConversations(() => {});
    await flush();
  }

  beforeEach(() => {
    steerChat.mockRestore(); // the real client, so the request body is what gets checked
    unsubscribe = () => {};
    stubSteerFetch(accepted);
  });

  afterEach(() => {
    unsubscribe();
    resetConversations();
    vi.unstubAllGlobals();
  });

  it("posts private: true (and no incognito) for a mid-turn steer into a session listed in the Conversations store", async () => {
    await listFromOthers(sessionId);
    const r = renderHook(baseOpts({ draft: "change course", session: { id: sessionId, incognito: true } }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toMatchObject({ text: "change course", private: true });
    // A private steer is text only: the console never adds incognito (or images) to it.
    expect(bodies[0]).not.toHaveProperty("incognito");
    expect(bodies[0]).not.toHaveProperty("images");
    expect(r.current.steerQueue).toMatchObject([{ text: "change course" }]);
  });

  it("carries pipeline context on a private steer without sending images", async () => {
    await listFromOthers(sessionId);
    const clearTrayContext = vi.fn();
    const r = renderHook(baseOpts({ draft: "use this", trayContext: () => PIPED, clearTrayContext }));
    await act(() => r.current.queueSteer());
    expect(bodies[0]).toMatchObject({ text: PIPED.contexts[0] + "\n\nuse this", private: true });
    expect(bodies[0]).not.toHaveProperty("images");
    expect(clearTrayContext).toHaveBeenCalledOnce();
    expect(r.current.steerQueue[0].text).toBe("use this\n\nAttached: Pasted text.txt");
  });

  it("an ordinary chat- session that is not in the store posts no private key", async () => {
    expect(sessionId.startsWith("chat-")).toBe(true);
    await listFromOthers("ctx-someone-else");
    const r = renderHook(baseOpts({ draft: "go left" }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).not.toHaveProperty("private");
  });

  it("with images in the tray, says a private message is text only and sends nothing — draft and tray untouched", async () => {
    await listFromOthers(sessionId);
    draftBox.value = "look at this";
    const onError = vi.fn();
    const clearTrayImages = vi.fn();
    const r = renderHook(baseOpts({ onError, trayImages: () => [IMG], clearTrayImages }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(bodies).toEqual([]);
    expect(onError).toHaveBeenCalledWith(PRIVATE_STEER_TEXT_ONLY);
    expect(draftBox.value).toBe("look at this");
    expect(clearTrayImages).not.toHaveBeenCalled();
    expect(r.current.steerQueue).toEqual([]);
  });

  // The server's own fixed refusal details (chat_routes._private_steer, chat.py's parked 409).
  const SERVER_DETAIL = {
    409: "This conversation is waiting on a decision a private steer can't answer. Answer the decision first.",
    400: "A private steer carries text only: no attachments, no incognito.",
    403: "Private steering is not available for this request.",
    503: "The private steer could not be delivered. Nothing was sent publicly.",
  } as const;

  // Listed: sent private, any (reworded) detail maps by status. Unlisted (outside the list's
  // window): sent ordinary, the server goes private on its own and its fixed detail maps.
  it.each([
    [true, 409, PRIVATE_STEER_WAITING_ON_PEER, "This conversation is waiting on the caller's own question"],
    [true, 400, PRIVATE_STEER_TEXT_ONLY, "A private steer is text only: no attachments or incognito"],
    [true, 403, PRIVATE_STEER_UNAVAILABLE, "Private steering is not available for this caller"],
    [false, 409, PRIVATE_STEER_WAITING_ON_PEER, SERVER_DETAIL[409]],
    [false, 400, PRIVATE_STEER_TEXT_ONLY, SERVER_DETAIL[400]],
    [false, 403, PRIVATE_STEER_UNAVAILABLE, SERVER_DETAIL[403]],
    [true, 503, PRIVATE_STEER_DELIVERY_FAILED, SERVER_DETAIL[503]],
    [false, 503, PRIVATE_STEER_DELIVERY_FAILED, SERVER_DETAIL[503]],
    [true, 502, PRIVATE_STEER_DELIVERY_FAILED, "upstream failed"],
    // A fleet member being down is not the thread waiting on the peer's question.
    [true, 409, PRIVATE_STEER_DELIVERY_FAILED, "agent 'scout' is not running"],
  ] as const)("listed=%s: a %i refusal shows its plain sentence and hands the text back", async (listed, status, sentence, detail) => {
    await listFromOthers(listed ? sessionId : "ctx-someone-else");
    stubSteerFetch(refused(status, detail));
    draftBox.value = "change course";
    const onError = vi.fn();
    const r = renderHook(baseOpts({ onError }));
    await act(() => r.current.queueSteer());
    await flush();
    expect("private" in bodies[0]).toBe(listed);
    expect(onError).toHaveBeenCalledTimes(1);
    const shown = onError.mock.calls[0][0] as string;
    expect(shown).toBe(sentence);
    expect(shown).not.toContain(String(status));
    expect(shown).not.toContain(detail);
    // The typed text is not lost: out of the queue, back in the composer.
    expect(draftBox.value).toBe("change course");
    expect(r.current.steerQueue).toEqual([]);
  });

  it("hands back a private steer after a network failure without overwriting a newer draft", async () => {
    await listFromOthers(sessionId);
    stubSteerFetch(() => {
      draftBox.value = "new words";
      resetConversations();
      throw new TypeError("Failed to fetch");
    });
    const onError = vi.fn();
    const r = renderHook(baseOpts({ draft: "change course", onError }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(onError).toHaveBeenCalledExactlyOnceWith(PRIVATE_STEER_DELIVERY_FAILED);
    expect(draftBox.value).toBe("new words\n\nchange course");
    expect(r.current.steerQueue).toEqual([]);
    expect(loadSteers(sessionId)).toEqual([]);
  });

  it("an ordinary chat keeps its own failure line for the same status", async () => {
    stubSteerFetch(refused(403, "nope"));
    const onError = vi.fn();
    const r = renderHook(baseOpts({ draft: "go", onError }));
    await act(() => r.current.queueSteer());
    await flush();
    expect(onError).toHaveBeenCalledWith("Couldn't queue message: nope");
  });

  it.each([
    ["listed refusal", false, refused(409, SERVER_DETAIL[409]), PRIVATE_STEER_WAITING_ON_PEER],
    ["removed while pending", true, refused(409, SERVER_DETAIL[409]), PRIVATE_STEER_WAITING_ON_PEER],
    ["delivery failure", true, refused(503, SERVER_DETAIL[503]), PRIVATE_STEER_DELIVERY_FAILED],
    ["network failure", true, () => { throw new TypeError("Failed to fetch"); }, PRIVATE_STEER_DELIVERY_FAILED],
  ] as const)("a private re-queue hands back its text: %s", async (_label, removeWhilePending, reply, sentence) => {
    await listFromOthers(sessionId);
    // A stranded interjection whose turn ended; a turn starts in this slot WHILE the reconcile
    // dequeues it, so resendInterjections hands it to that running turn as a steer
    // (requeueIntoLiveTurn) instead of opening a second stream.
    saveSteers(sessionId, [{ id: "s1", text: "late words", serverTaskId: "task-gone" }]);
    pendingSteer.mockResolvedValue({ pending: [{ id: "s1", text: "late words" }], drained: [] });
    taskSteerState.mockResolvedValue({ state: "completed", consumed: [] });
    cancelSteer.mockImplementation(async () => {
      chatStore.setSessionStatus(sessionId, "streaming");
      return { removed: true, pending: 0 };
    });
    const onError = vi.fn();
    const runTurn = mkRun();
    stubSteerFetch(() => {
      if (removeWhilePending) resetConversations();
      return reply();
    });
    renderHook(baseOpts({ onError, runTurn, status: "idle" }));
    await flush();
    expect(runTurn).not.toHaveBeenCalled();
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toMatchObject({ id: "s1", text: "late words", private: true });
    expect(onError).toHaveBeenCalledWith(sentence);
    expect(draftBox.value).toBe("late words");
    chatStore.setSessionStatus(sessionId, "idle");
  });

  it("renders no extra control for a From others session compared with an ordinary chat", async () => {
    // Ordinary chat first: what the hook renders and what it hands the slot to render from.
    const ordinary = renderHook(baseOpts({ draft: "go" }));
    await act(() => ordinary.current.queueSteer());
    await flush();
    const ordinaryHtml = container.innerHTML;
    const ordinaryKeys = Object.keys(ordinary.current).sort();
    const ordinaryItemKeys = Object.keys(ordinary.current.steerQueue[0]).sort();
    act(() => root.unmount());
    root = createRoot(container);

    await listFromOthers(sessionId);
    window.sessionStorage.clear();
    const peer = renderHook(baseOpts({ draft: "go" }));
    await act(() => peer.current.queueSteer());
    await flush();
    expect(bodies[1]).toMatchObject({ private: true });
    // Same (empty) DOM, same hook surface, same queued-bubble shape: no toggle, no badge,
    // no flag for the slot to draw one from.
    expect(container.innerHTML).toBe(ordinaryHtml);
    expect(container.querySelector("button, input, [role='switch'], [role='checkbox']")).toBeNull();
    expect(Object.keys(peer.current).sort()).toEqual(ordinaryKeys);
    expect(Object.keys(peer.current.steerQueue[0]).sort()).toEqual(ordinaryItemKeys);
  });
});
