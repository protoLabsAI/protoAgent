import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";

const mocks = vi.hoisted(() => {
  const sessions = [{ id: "s1", messages: [] as ChatMessage[] }];
  const updateMessages = vi.fn((sessionId: string, messages: ChatMessage[]) => {
    const session = sessions.find((s) => s.id === sessionId);
    if (session) session.messages = messages;
  });
  return {
    handlers: new Map<string, (data: Record<string, unknown>) => void>(),
    sessions,
    updateMessages,
    setServerTurnControl: vi.fn(),
    clearServerTurnControl: vi.fn(),
  };
});

vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    mocks.handlers.set(topic, fn);
    return () => mocks.handlers.delete(topic);
  },
}));

vi.mock("../chat/chat-store", () => ({
  chatStore: {
    getSnapshot: () => ({ sessions: mocks.sessions }),
    updateMessages: mocks.updateMessages,
    setServerTurnControl: mocks.setServerTurnControl,
    clearServerTurnControl: mocks.clearServerTurnControl,
  },
}));

import { busMayFold, parseServerTurnControl, ServerTurnWatch } from "./ServerTurnWatch";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("parseServerTurnControl", () => {
  it("requires a session and durable task id", () => {
    expect(
      parseServerTurnControl({
        session_id: "s1",
        task_id: "task-1",
        origin: "scheduler",
        trigger: "job-1",
        operator_controllable: true,
      }),
    ).toEqual({
      session_id: "s1",
      task_id: "task-1",
      origin: "scheduler",
      trigger: "job-1",
      controllable: true,
      operator_controllable: true,
    });
    expect(parseServerTurnControl({ session_id: "s1", controllable: true })).toBeNull();
    expect(parseServerTurnControl(null)).toBeNull();
  });

  it("does not infer operator control when the server explicitly withholds it", () => {
    expect(
      parseServerTurnControl({
        session_id: "s1",
        task_id: "task-1",
        controllable: true,
        operator_controllable: false,
      }),
    ).toMatchObject({ controllable: true, operator_controllable: false });
  });
});

describe("ServerTurnWatch control payload bridge", () => {
  let root: Root | null = null;
  let node: HTMLDivElement;

  beforeEach(() => {
    mocks.handlers.clear();
    mocks.updateMessages.mockClear();
    mocks.setServerTurnControl.mockClear();
    mocks.clearServerTurnControl.mockClear();
    mocks.sessions[0].messages = [];
    node = document.createElement("div");
    document.body.appendChild(node);
    root = createRoot(node);
  });

  afterEach(() => {
    act(() => root?.unmount());
    node.remove();
    root = null;
  });

  it("relays control payloads from server progress without rendering turn_started", () => {
    const seen: unknown[] = [];
    const onControl = (event: Event) => {
      seen.push((event as CustomEvent).detail);
    };
    window.addEventListener("protoagent:server-turn-control", onControl);

    act(() => root?.render(h(ServerTurnWatch)));
    act(() =>
      mocks.handlers.get("chat.progress")?.({
        session_id: "s1",
        task_id: "task-1",
        phase: "turn_started",
        control: {
          session_id: "s1",
          task_id: "task-1",
          origin: "background-resume",
          trigger: "bg-1",
          operator_controllable: true,
        },
      }),
    );

    expect(seen).toEqual([
      {
        session_id: "s1",
        task_id: "task-1",
        origin: "background-resume",
        trigger: "bg-1",
        controllable: true,
        operator_controllable: true,
      },
    ]);
    expect(mocks.setServerTurnControl).toHaveBeenCalledWith({
      sessionId: "s1",
      taskId: "task-1",
      origin: "background-resume",
      trigger: "bg-1",
      controllable: true,
      operatorControllable: true,
    });
    expect(mocks.updateMessages).not.toHaveBeenCalled();
    window.removeEventListener("protoagent:server-turn-control", onControl);
  });

  it("stores non-interactive control frames as a clear for the session", () => {
    act(() => root?.render(h(ServerTurnWatch)));
    act(() =>
      mocks.handlers.get("turn.started")?.({
        session_id: "s1",
        origin: "scheduler",
        control: {
          session_id: "s1",
          task_id: "task-1",
          origin: "scheduler",
          controllable: false,
          operator_controllable: false,
        },
      }),
    );

    expect(mocks.setServerTurnControl).toHaveBeenCalledWith({
      sessionId: "s1",
      taskId: "task-1",
      origin: "scheduler",
      trigger: "",
      controllable: false,
      operatorControllable: false,
    });
  });

  it("clears the stored control for the finished durable task when the server turn finishes", () => {
    act(() => root?.render(h(ServerTurnWatch)));
    act(() => mocks.handlers.get("turn.finished")?.({ session_id: "s1", task_id: "task-1" }));

    expect(mocks.clearServerTurnControl).toHaveBeenCalledWith("s1", "task-1");
  });

  it("does not clear a newer same-session control when an older server turn finishes late", () => {
    act(() => root?.render(h(ServerTurnWatch)));
    act(() =>
      mocks.handlers.get("turn.started")?.({
        session_id: "s1",
        origin: "scheduler",
        control: {
          session_id: "s1",
          task_id: "task-new",
          origin: "scheduler",
          controllable: true,
          operator_controllable: true,
        },
      }),
    );
    act(() => mocks.handlers.get("turn.finished")?.({ session_id: "s1", task_id: "task-old" }));

    expect(mocks.setServerTurnControl).toHaveBeenCalledWith({
      sessionId: "s1",
      taskId: "task-new",
      origin: "scheduler",
      trigger: "",
      controllable: true,
      operatorControllable: true,
    });
    expect(mocks.clearServerTurnControl).toHaveBeenCalledWith("s1", "task-old");
  });

  it("keeps legacy session-level cleanup for finish events without a task id", () => {
    act(() => root?.render(h(ServerTurnWatch)));
    act(() => mocks.handlers.get("turn.finished")?.({ session_id: "s1" }));

    expect(mocks.clearServerTurnControl).toHaveBeenCalledWith("s1");
  });

  it("an un-addressed finish does NOT clear the control while another server turn is still live", () => {
    // Two nudges overlap on one session: the A2A server serializes the turns, but the
    // second's control frame arrives while the first is still running, and an older server
    // can't say WHICH turn just finished. Clearing whatever control is there dropped the
    // LIVE turn's — and with it the operator's queued interjection, which then had nothing
    // to settle it. The indicator's own ref-count is the guard.
    act(() => root?.render(h(ServerTurnWatch)));
    act(() => mocks.handlers.get("turn.started")?.({ session_id: "s1", origin: "background-resume" }));
    act(() => mocks.handlers.get("turn.started")?.({ session_id: "s1", origin: "background-resume" }));

    act(() => mocks.handlers.get("turn.finished")?.({ session_id: "s1" }));
    expect(mocks.clearServerTurnControl).not.toHaveBeenCalled();

    // The last one out clears it.
    act(() => mocks.handlers.get("turn.finished")?.({ session_id: "s1" }));
    expect(mocks.clearServerTurnControl).toHaveBeenCalledWith("s1");
  });

  it("an ADDRESSED finish clears only that turn's control, whoever else is live", () => {
    act(() => root?.render(h(ServerTurnWatch)));
    act(() => mocks.handlers.get("turn.started")?.({ session_id: "s1", origin: "background-resume" }));
    act(() => mocks.handlers.get("turn.started")?.({ session_id: "s1", origin: "background-resume" }));
    act(() => mocks.handlers.get("turn.finished")?.({ session_id: "s1", task_id: "task-1" }));

    expect(mocks.clearServerTurnControl).toHaveBeenCalledWith("s1", "task-1");
  });

  it("keeps ordinary progress rendering intact", () => {
    act(() => root?.render(h(ServerTurnWatch)));
    act(() =>
      mocks.handlers.get("chat.progress")?.({
        session_id: "s1",
        task_id: "task-1",
        phase: "text",
        text: "working",
      }),
    );

    expect(mocks.updateMessages).toHaveBeenCalledOnce();
    expect(mocks.sessions[0].messages[0].content).toBe("working");
  });
});

describe("busMayFold — one producer per bubble, with two exceptions", () => {
  it("stands aside for a reattach on the frames a reattach also writes", () => {
    expect(busMayFold("text", true)).toBe(false);
    expect(busMayFold("tool", true)).toBe(false);
    expect(busMayFold("text", false)).toBe(true);
  });

  it("always folds a consumed-interjection marker — no reattach ever places one", () => {
    // The reattach stream drops steer markers (snapshot replay can't reconstruct the
    // boundary), so if the bus stood aside here NOTHING would settle the operator's
    // message and it would sit queued under an answer that had already used it.
    expect(busMayFold("steer", true)).toBe(true);
    expect(busMayFold("steer", false)).toBe(true);
    // Same reasoning as the room bubble, which no reattach drives either.
    expect(busMayFold("room", true)).toBe(true);
  });
});
