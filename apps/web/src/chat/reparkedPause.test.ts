// A pause moved to a new task by a plain message must stay the chat's LIVE turn (#3963).
//
// A session parked on `ask_human`; a plain message (no hitl_resume) was held and re-parked
// the pause on a NEW task, and the old task completed ("Continued in task …") ~50 ms later.
// The durable turns came back ordered by when each row last changed — the still-parked task
// FIRST — and hydration drew them in that order: the completed bubble ended the transcript,
// nothing reattached, and no form came back in a fresh profile. In a warm profile the tab
// reattached to the OLD task, found it complete and settled it: the form was gone there too.
//
// Drives the real hydration, the real reattach and the real chatStore; only the api
// transport is mocked.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, supersededByFromStatus, type DurableChatSession, type DurableChatTurn } from "../lib/api";
import { chatStore } from "./chat-store";
import { reattachKeyForMessages, reattachTurn } from "./reattach";
import { hydrateDurableChatSessions, orderDurableTurns, sessionFromDurableTurns } from "./sessionHydration";

vi.mock("../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api")>();
  return {
    ...mod,
    api: {
      ...mod.api,
      resumeTask: vi.fn(),
      getTask: vi.fn(),
      replayTask: vi.fn(),
      getTaskTurn: vi.fn(),
      chatSessions: vi.fn(),
      chatSessionTurns: vi.fn(),
    },
  };
});

const HITL = "application/vnd.protolabs.hitl-v1+json";
const TOOL = "https://proto-labs.ai/a2a/ext/tool-call-v1";
const QUESTION = "What is your favourite fruit?";
const OLD = "task-old";
const NEW = "task-new";

/** The first task: asked, parked on ask_human, then superseded — completed with a pointer,
 *  stamped AFTER the task that took its pause over. */
function superseded(overrides: Partial<DurableChatTurn> = {}): DurableChatTurn {
  return {
    task_id: OLD,
    state: "TASK_STATE_COMPLETED",
    last_updated: "2026-09-30T21:37:50.143002",
    text: "",
    status: {
      state: "TASK_STATE_COMPLETED",
      message: {
        role: "ROLE_AGENT",
        parts: [{ text: `Continued in task ${NEW}.` }],
        metadata: { protoagent_superseded_by: NEW },
      },
    },
    artifacts: [],
    history: [
      { role: "ROLE_USER", parts: [{ text: "Ask me my favourite fruit." }] },
      {
        role: "ROLE_AGENT",
        parts: [],
        metadata: { [TOOL]: { toolCallId: "ask-1", name: "ask_human", phase: "started", args: "{}" } },
      },
    ],
    ...overrides,
  };
}

/** The task the held message re-parked the pause on. */
function reparked(overrides: Partial<DurableChatTurn> = {}): DurableChatTurn {
  return {
    task_id: NEW,
    state: "TASK_STATE_INPUT_REQUIRED",
    last_updated: "2026-09-30T21:37:50.093746",
    text: "",
    status: {
      state: "TASK_STATE_INPUT_REQUIRED",
      message: { role: "ROLE_AGENT", parts: [{ data: { question: QUESTION }, metadata: { mimeType: HITL } }] },
    },
    artifacts: [],
    history: [{ role: "ROLE_USER", parts: [{ text: "Also end your next reply with HELD." }] }],
    ...overrides,
  };
}

const summary = (id: string): DurableChatSession => ({ session_id: id, last_updated: "2026-09-30T21:37:50Z", turn_count: 2 });

const lastAssistant = (messages: { role: string }[]) => [...messages].reverse().find((m) => m.role === "assistant");

afterEach(() => vi.clearAllMocks());

describe("hydration draws the re-parked pause as the live turn (#3963)", () => {
  it("puts the parked turn LAST when the server lists it first (a server ordering by last change)", () => {
    // What main served: the parked task first, the completion stamped after it last.
    const session = sessionFromDurableTurns(summary("chat-a"), [reparked(), superseded()]);
    if (!session) throw new Error("expected a session");
    const last = lastAssistant(session.messages);
    expect(last).toMatchObject({ taskId: NEW, status: "streaming", paused: true });
    // The chat reads in the order it happened: the question, then the held message.
    expect(session.messages.filter((m) => m.role === "user").map((m) => m.content)).toEqual([
      "Ask me my favourite fruit.",
      "Also end your next reply with HELD.",
    ]);
    // Exactly one turn waits: the superseded one is over.
    expect(session.messages.filter((m) => m.paused)).toHaveLength(1);
    expect(session.messages.find((m) => m.taskId === OLD)?.status).toBe("done");
    // So the slot's reattach resubscribes to the task that holds the pause.
    expect(reattachKeyForMessages(session.messages)).toBe(`durable-${NEW}-assistant:${NEW}`);
  });

  it("follows the server's live marker over list position, and settles a stale second pause", () => {
    // The ~50 ms window: both rows still input-required, the old one listed last.
    const stale = superseded({
      state: "TASK_STATE_INPUT_REQUIRED",
      status: reparked().status,
    });
    const turns = orderDurableTurns([reparked(), stale], NEW);
    expect(turns.map((t) => [t.task_id, t.state])).toEqual([
      [OLD, "TASK_STATE_COMPLETED"],
      [NEW, "TASK_STATE_INPUT_REQUIRED"],
    ]);
    const session = sessionFromDurableTurns(summary("chat-b"), [reparked(), stale], NEW);
    expect(session?.messages.filter((m) => m.paused).map((m) => m.taskId)).toEqual([NEW]);
  });

  it("leaves a finished chat in its own order when the server says nothing is live", () => {
    const done = reparked({ state: "TASK_STATE_COMPLETED", status: { state: "TASK_STATE_COMPLETED" } });
    expect(orderDurableTurns([superseded(), done], null).map((t) => t.task_id)).toEqual([OLD, NEW]);
  });

  it("keeps the RUNNING turn live when a newer turn is queued behind it", () => {
    // A turn is created (and marked working) before it waits for the session's lock, so a
    // queued one is the newer row; the server names the running one live.
    const done = superseded({ task_id: "task-done", status: { state: "TASK_STATE_COMPLETED" }, history: [{ role: "ROLE_USER", parts: [{ text: "earlier" }] }] });
    const running = reparked({
      task_id: "task-running",
      state: "TASK_STATE_WORKING",
      status: { state: "TASK_STATE_WORKING" },
      artifacts: [{ parts: [{ text: "half an answer" }] }],
      history: [{ role: "ROLE_USER", parts: [{ text: "the running question" }] }],
    });
    const queued = reparked({
      task_id: "task-queued",
      state: "TASK_STATE_WORKING",
      status: { state: "TASK_STATE_WORKING" },
      history: [{ role: "ROLE_USER", parts: [{ text: "a queued nudge" }] }],
    });
    const session = sessionFromDurableTurns(summary("chat-q"), [done, running, queued], "task-running");
    if (!session) throw new Error("expected a session");
    // Chronology kept: the queued prompt comes after the running turn…
    expect(session.messages.filter((m) => m.role === "user").map((m) => m.content)).toEqual([
      "earlier",
      "the running question",
      "a queued nudge",
    ]);
    // …but it has said nothing, so the running turn owns the live bubble the reattach follows.
    expect(session.messages.filter((m) => m.taskId === "task-queued")).toHaveLength(0);
    expect(lastAssistant(session.messages)).toMatchObject({ taskId: "task-running", status: "streaming" });
    expect(reattachKeyForMessages(session.messages)).toBe("durable-task-running-assistant:task-running");
  });

  it("without a marker, never moves an older orphan that merely never ended", () => {
    // An older server, and a row left working under a completed turn that is NOT a
    // superseded pause: nothing says it is live, so the chat keeps its order.
    const orphan = reparked({ task_id: "task-orphan", state: "TASK_STATE_WORKING", status: { state: "TASK_STATE_WORKING" } });
    const later = superseded({ task_id: "task-later", status: { state: "TASK_STATE_COMPLETED" } });
    expect(orderDurableTurns([orphan, later]).map((t) => t.task_id)).toEqual(["task-orphan", "task-later"]);
  });

  it("boot hydration hands the server's live marker through", async () => {
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ sessions: [] } as never);
    const commit = vi.spyOn(chatStore, "hydrateSessions").mockImplementation(() => {});
    vi.mocked(api.chatSessions).mockResolvedValue({ sessions: [summary("chat-c")] });
    // A stale pause listed LAST: only the marker can say which one is live.
    const stale = superseded({ state: "TASK_STATE_INPUT_REQUIRED", status: reparked().status });
    vi.mocked(api.chatSessionTurns).mockResolvedValue({ turns: [reparked(), stale], live_task_id: NEW });

    await hydrateDurableChatSessions();

    const [sessions] = commit.mock.calls[0];
    expect(lastAssistant(sessions[0].messages)).toMatchObject({ taskId: NEW, paused: true });
    vi.restoreAllMocks();
  });
});

describe("a warm tab reattaching to the superseded task follows it to the new one (#3963)", () => {
  beforeEach(() => {
    vi.mocked(api.resumeTask).mockRejectedValue(new Error("task is not running (UnsupportedOperationError)"));
    vi.mocked(api.replayTask).mockResolvedValue("TASK_STATE_COMPLETED");
    vi.mocked(api.getTask).mockResolvedValue({ state: "TASK_STATE_COMPLETED", text: "", supersededBy: NEW });
    vi.mocked(api.getTaskTurn).mockImplementation(async (id) => (id === NEW ? reparked() : null));
  });

  it("settles the old bubble and draws the re-parked turn after it, streaming + paused", async () => {
    const session = chatStore.createSession();
    chatStore.updateMessages(session.id, [
      { id: "u1", role: "user", content: "Ask me my favourite fruit.", status: "done" },
      {
        id: "a1",
        role: "assistant",
        content: "",
        status: "streaming",
        paused: true,
        taskId: OLD,
        toolCalls: [{ id: "ask-1", name: "ask_human", status: "running", paused: true }],
      },
    ]);
    const cancel = reattachTurn(session.id, "a1", OLD);
    await vi.waitFor(() => {
      const messages = chatStore.getSnapshot().sessions.find((s) => s.id === session.id)!.messages;
      expect(lastAssistant(messages)).toMatchObject({ taskId: NEW, status: "streaming", paused: true });
    });
    const messages = chatStore.getSnapshot().sessions.find((s) => s.id === session.id)!.messages;
    const old = messages.find((m) => m.id === "a1")!;
    expect(old.status).toBe("done");
    expect(old.paused).toBeUndefined();
    expect(old.toolCalls?.every((c) => c.status === "done")).toBe(true);
    expect(messages.filter((m) => m.role === "user").map((m) => m.content)).toEqual([
      "Ask me my favourite fruit.",
      "Also end your next reply with HELD.",
    ]);
    // The slot's reattach key moves to the new task: that is what brings the form back.
    expect(reattachKeyForMessages(messages)).toBe(`durable-${NEW}-assistant:${NEW}`);
    cancel();
  });

  it("does not draw a successor this console already shows", async () => {
    const session = chatStore.createSession();
    chatStore.updateMessages(session.id, [
      { id: "a1", role: "assistant", content: "", status: "streaming", paused: true, taskId: OLD },
      { id: "u2", role: "user", content: "Also end your next reply with HELD.", status: "done" },
      { id: "a2", role: "assistant", content: "", status: "streaming", paused: true, taskId: NEW },
    ]);
    const cancel = reattachTurn(session.id, "a1", OLD);
    await vi.waitFor(() => {
      const messages = chatStore.getSnapshot().sessions.find((s) => s.id === session.id)!.messages;
      expect(messages.find((m) => m.id === "a1")?.status).toBe("done");
    });
    const messages = chatStore.getSnapshot().sessions.find((s) => s.id === session.id)!.messages;
    expect(messages.map((m) => m.id)).toEqual(["a1", "u2", "a2"]);
    cancel();
  });
});

describe("the supersede pointer", () => {
  it("reads the metadata, falls back to the text an older server wrote, and ignores live tasks", () => {
    expect(supersededByFromStatus(superseded().status)).toBe(NEW);
    expect(
      supersededByFromStatus({
        state: "TASK_STATE_COMPLETED",
        message: { parts: [{ text: `Continued in task ${NEW}.` }] },
      }),
    ).toBe(NEW);
    expect(supersededByFromStatus({ state: "TASK_STATE_COMPLETED", message: { parts: [{ text: "Done." }] } })).toBeUndefined();
    expect(supersededByFromStatus(reparked().status)).toBeUndefined();
  });
});
