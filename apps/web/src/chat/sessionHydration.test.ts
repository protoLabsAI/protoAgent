import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type DurableChatSession, type DurableChatTurn } from "../lib/api";
import { chatStore, DEFAULT_SESSION_TITLE, mergeHydratedSessions, type ChatSession } from "./chat-store";
import {
  HYDRATION_CONCURRENCY,
  hydrateDurableChatSessions,
  messagesFromDurableTurn,
  sessionFromDurableTurns,
} from "./sessionHydration";

const TOOL = "https://proto-labs.ai/a2a/ext/tool-call-v1";
const COST = "https://proto-labs.ai/a2a/ext/cost-v1";
const REASONING = "application/vnd.protolabs.reasoning-v1+json";
const COMPONENT = "application/vnd.protolabs.component-v1+json";
const CONTEXT = "application/vnd.protolabs.context-v1+json";

function turn(overrides: Partial<DurableChatTurn> = {}): DurableChatTurn {
  return {
    task_id: "task-1",
    state: "TASK_STATE_COMPLETED",
    last_updated: "2026-08-20T12:00:00Z",
    text: "answer",
    status: { state: "TASK_STATE_COMPLETED" },
    artifacts: [{ parts: [{ text: "answer" }] }],
    history: [{ role: "ROLE_USER", parts: [{ text: "How do I ship this?" }] }],
    ...overrides,
  };
}

function summary(id = "chat-server"): DurableChatSession {
  return { session_id: id, last_updated: "2026-08-20T12:00:00Z", turn_count: 1 };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

afterEach(() => vi.restoreAllMocks());

describe("durable turn conversion", () => {
  it("rebuilds the user bubble and drives assistant text/tools through shared reducers", () => {
    const messages = messagesFromDurableTurn(
      turn({
        history: [
          { role: "ROLE_USER", parts: [{ text: "How do I ship this?" }] },
          {
            role: "ROLE_AGENT",
            parts: [],
            metadata: { [TOOL]: { toolCallId: "call-1", name: "run_command", phase: "started", args: "npm test" } },
          },
          {
            role: "ROLE_AGENT",
            parts: [],
            metadata: { [TOOL]: { toolCallId: "call-1", name: "run_command", phase: "completed", result: "ok" } },
          },
        ],
      }),
    );
    expect(messages[0]).toMatchObject({ role: "user", content: "How do I ship this?", status: "done" });
    expect(messages[1]).toMatchObject({
      role: "assistant",
      content: "answer",
      status: "done",
      taskId: "task-1",
      toolCalls: [{ id: "call-1", name: "run_command", input: "npm test", output: "ok", status: "done" }],
    });
  });

  it("replays reasoning, components, cost, and context through the shared snapshot path", () => {
    const messages = messagesFromDurableTurn(
      turn({
        history: [
          { role: "ROLE_USER", parts: [{ text: "Show the release" }] },
          { role: "ROLE_AGENT", parts: [{ data: { text: "checking" }, metadata: { mimeType: REASONING } }] },
          {
            role: "ROLE_AGENT",
            parts: [{ data: { component: "key-value", props: { version: "1.2.3" } }, metadata: { mimeType: COMPONENT } }],
          },
        ],
        artifacts: [{
          parts: [
            { text: "ready" },
            { data: { contextTokens: 1200, maxTokens: 8000 }, metadata: { mimeType: CONTEXT } },
          ],
          metadata: {
            [COST]: { usage: { input_tokens: 100, output_tokens: 20 }, costUsd: 0.012, durationMs: 450 },
          },
        }],
      }),
    );
    expect(messages[1]).toMatchObject({
      content: "ready",
      reasoning: "checking",
      components: [{ component: "key-value", props: { version: "1.2.3" } }],
      usage: { inputTokens: 100, outputTokens: 20, totalTokens: 120, costUsd: 0.012, durationMs: 450 },
      contextWindow: { contextTokens: 1200, maxTokens: 8000 },
    });
  });

  it("keeps a nonterminal assistant reattachable", () => {
    const messages = messagesFromDurableTurn(
      turn({ state: "TASK_STATE_WORKING", status: { state: "TASK_STATE_WORKING" } }),
    );
    expect(messages[messages.length - 1]).toMatchObject({
      role: "assistant",
      content: "",
      status: "streaming",
      taskId: "task-1",
    });
    expect(messages[messages.length - 1]).not.toHaveProperty("reasoning");
  });

  it("derives a fixed-id session and title from the first durable prompt", () => {
    const session = sessionFromDurableTurns(summary(), [turn()]);
    expect(session).toMatchObject({ id: "chat-server", title: "How do I ship this?" });
    expect(session?.messages).toHaveLength(2);
  });

  it("falls back to the default title when a server turn has no visible user text", () => {
    const session = sessionFromDurableTurns(summary(), [turn({ history: [] })]);
    expect(session?.title).toBe(DEFAULT_SESSION_TITLE);
  });

  it("restores incognito from the newest durable operator message", () => {
    const privateTurn = turn({
      task_id: "task-private",
      history: [{ role: "ROLE_USER", parts: [{ text: "private" }], metadata: { incognito: true } }],
    });
    expect(sessionFromDurableTurns(summary(), [privateTurn])?.incognito).toBe(true);

    const laterOrdinaryTurn = turn({
      task_id: "task-ordinary",
      last_updated: "2026-08-20T12:01:00Z",
      history: [{ role: "ROLE_USER", parts: [{ text: "ordinary now" }] }],
    });
    expect(sessionFromDurableTurns(summary(), [privateTurn, laterOrdinaryTurn])?.incognito).toBeUndefined();
  });
});

describe("boot hydration", () => {
  it("inherits recovered incognito for an existing empty tab without a local choice", () => {
    const existing = {
      id: "chat-private",
      title: DEFAULT_SESSION_TITLE,
      messages: [],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    const recovered = { ...existing, messages: [{ role: "user", content: "secret" }], incognito: true } as ChatSession;
    const current = {
      version: 1,
      sessions: [existing],
      currentSessionId: existing.id,
      activeSessions: [existing.id],
      sessionStatusMap: {},
      pendingDeleteRequest: null,
      pendingClearRequest: null,
    };
    expect(mergeHydratedSessions(current, [recovered]).sessions[0].incognito).toBe(true);
  });

  it("fetches only missing/empty sessions, tolerates one failure, and commits successful siblings", async () => {
    const nonEmpty = {
      id: "chat-local",
      title: "Local",
      messages: [{ role: "user", content: "local" }],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    const empty = { id: "chat-empty", title: "Empty", messages: [], createdAt: 1, updatedAt: 1 } as ChatSession;
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ sessions: [nonEmpty, empty] } as never);
    const commit = vi.spyOn(chatStore, "hydrateSessions").mockImplementation(() => {});
    vi.spyOn(api, "chatSessions").mockResolvedValue({
      sessions: [summary("chat-local"), summary("chat-empty"), summary("chat-new"), summary("chat-fails")],
    });
    const reads = vi.spyOn(api, "chatSessionTurns").mockImplementation(async (id) => {
      if (id === "chat-fails") throw new Error("member cold");
      return { turns: [turn({ task_id: `task-${id}` })] };
    });

    await hydrateDurableChatSessions();

    expect(reads.mock.calls.map(([id]) => id).sort()).toEqual(["chat-empty", "chat-fails", "chat-new"]);
    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit.mock.calls[0][0].map((session) => session.id).sort()).toEqual(["chat-empty", "chat-new"]);
  });

  it("never exceeds the bounded fetch fan-out", async () => {
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ sessions: [] } as never);
    vi.spyOn(chatStore, "hydrateSessions").mockImplementation(() => {});
    vi.spyOn(api, "chatSessions").mockResolvedValue({
      sessions: Array.from({ length: 12 }, (_, i) => summary(`chat-${i}`)),
    });
    let active = 0;
    let peak = 0;
    vi.spyOn(api, "chatSessionTurns").mockImplementation(async (id) => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise((resolve) => setTimeout(resolve, 1));
      active -= 1;
      return { turns: [turn({ task_id: `task-${id}` })] };
    });

    await hydrateDurableChatSessions();
    expect(peak).toBe(HYDRATION_CONCURRENCY);
  });

  it("does not resurrect a session deleted while its durable turns are in flight", async () => {
    const session = chatStore.createSession();
    const pending = deferred<{ turns: DurableChatTurn[] }>();
    vi.spyOn(api, "chatSessions").mockResolvedValue({ sessions: [summary(session.id)] });
    vi.spyOn(api, "chatSessionTurns").mockReturnValue(pending.promise);

    const hydration = hydrateDurableChatSessions();
    await vi.waitFor(() => expect(api.chatSessionTurns).toHaveBeenCalled());
    chatStore.deleteSession(session.id);
    pending.resolve({ turns: [turn()] });
    await hydration;

    expect(chatStore.getSnapshot().sessions.some((candidate) => candidate.id === session.id)).toBe(false);
  });

  it("does not overwrite a clear performed while durable turns are in flight", async () => {
    const session = chatStore.createSession();
    const pending = deferred<{ turns: DurableChatTurn[] }>();
    vi.spyOn(api, "chatSessions").mockResolvedValue({ sessions: [summary(session.id)] });
    vi.spyOn(api, "chatSessionTurns").mockReturnValue(pending.promise);

    const hydration = hydrateDurableChatSessions();
    await vi.waitFor(() => expect(api.chatSessionTurns).toHaveBeenCalled());
    chatStore.updateMessages(session.id, []);
    pending.resolve({ turns: [turn()] });
    await hydration;

    expect(chatStore.getSnapshot().sessions.find((candidate) => candidate.id === session.id)?.messages).toEqual([]);
  });
});
