import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type DurableChatSession, type DurableChatTurn } from "../lib/api";
import {
  chatStore,
  DEFAULT_SESSION_TITLE,
  mergeHydratedSessions,
  needsDurableHydration,
  type ChatSession,
} from "./chat-store";
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
        // `text` is the server's join of the artifact text parts
        // (operator_api/chat_routes.py `_text`), so it MUST match them: leaving the
        // fixture's default "answer" against a "ready" artifact described a response
        // the server cannot produce, and #3340's reconciliation reasonably trusts
        // `text` over the replay.
        text: "ready",
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

  it("keeps the rehydrated reply below the tool cards when the replay surfaces no answer text (#3340)", () => {
    // A completed turn the operator switched away from and returned to: the tool
    // frames live in `history`, and the durable store's joined answer is in `text`,
    // but the answer artifact part is not one the snapshot replay reads as text
    // (the console's `textFromParts` is stricter than the server's join, which
    // ignores `kind`). Before #3340 the bubble rehydrated with the tool cards but
    // NO reply — ChatMessageView draws a parts-bearing bubble from its ordered
    // parts and never falls back to `content`, so the answer vanished on return.
    const messages = messagesFromDurableTurn(
      turn({
        text: "Shipped — the release is live.",
        artifacts: [{ parts: [{ kind: "data", text: "Shipped — the release is live.", data: { ok: true } }] }],
        history: [
          { role: "ROLE_USER", parts: [{ text: "Ship the release" }] },
          {
            role: "ROLE_AGENT",
            parts: [],
            metadata: { [TOOL]: { toolCallId: "call-1", name: "run_command", phase: "started", args: "make release" } },
          },
          {
            role: "ROLE_AGENT",
            parts: [],
            metadata: { [TOOL]: { toolCallId: "call-1", name: "run_command", phase: "completed", result: "done" } },
          },
        ],
      }),
    );
    const assistant = messages[messages.length - 1];
    expect(assistant).toMatchObject({
      role: "assistant",
      status: "done",
      content: "Shipped — the release is live.",
      toolCalls: [{ id: "call-1", name: "run_command", status: "done" }],
    });
    // The reply must be an ORDERED trailing text run AFTER the tool group — a bubble
    // that keeps only the tool cards is exactly the regression.
    const parts = assistant.parts ?? [];
    expect(parts.some((part) => part.kind === "tools")).toBe(true);
    expect(parts[parts.length - 1]).toMatchObject({ kind: "text", text: "Shipped — the release is live." });
  });

  it("recovers the reply prose while preserving component/table output on rehydrate (#3340)", () => {
    // r2: the persisted turn rendered a component (table/timeline/key-value) AND
    // trailing prose. The component must survive the switch/return, and the prose
    // must land as the trailing answer run below it — not be dropped with `content`.
    const messages = messagesFromDurableTurn(
      turn({
        text: "Here is the latest release.",
        artifacts: [{ parts: [{ kind: "data", text: "Here is the latest release.", data: {} }] }],
        history: [
          { role: "ROLE_USER", parts: [{ text: "show the release" }] },
          {
            role: "ROLE_AGENT",
            parts: [{ data: { component: "key-value", props: { version: "1.2.3" } }, metadata: { mimeType: COMPONENT } }],
          },
        ],
      }),
    );
    const assistant = messages[messages.length - 1];
    expect(assistant.content).toBe("Here is the latest release.");
    expect(assistant.components).toEqual([{ component: "key-value", props: { version: "1.2.3" } }]);
    const parts = assistant.parts ?? [];
    expect(parts.some((part) => part.kind === "component")).toBe(true);
    expect(parts[parts.length - 1]).toMatchObject({ kind: "text", text: "Here is the latest release." });
  });

  it("keeps a nonterminal assistant reattachable", () => {
    const messages = messagesFromDurableTurn(
      turn({ state: "TASK_STATE_WORKING", status: { state: "TASK_STATE_WORKING" } }),
    );
    expect(messages[messages.length - 1]).toMatchObject({
      role: "assistant",
      content: "answer",
      status: "streaming",
      taskId: "task-1",
      durableSnapshotFallback: true,
    });
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

  it("retains rendered assistant text when switching back to a hydrated durable tab (#3340)", () => {
    const original = {
      id: "chat-original",
      title: DEFAULT_SESSION_TITLE,
      messages: [],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    const other = {
      id: "chat-other",
      title: "Other agent",
      messages: [
        { id: "other-user", role: "user", content: "meanwhile", status: "done" },
      ],
      createdAt: 2,
      updatedAt: 2,
    } as ChatSession;
    const recovered = sessionFromDurableTurns(summary(original.id), [
      turn({
        text: "Shipped — the release is live.",
        artifacts: [
          { parts: [{ kind: "data", text: "Shipped — the release is live.", data: { ok: true } }] },
        ],
        history: [
          { role: "ROLE_USER", parts: [{ text: "Ship the release" }] },
          {
            role: "ROLE_AGENT",
            parts: [],
            metadata: { [TOOL]: { toolCallId: "call-1", name: "run_command", phase: "started", args: "make release" } },
          },
          {
            role: "ROLE_AGENT",
            parts: [],
            metadata: { [TOOL]: { toolCallId: "call-1", name: "run_command", phase: "completed", result: "done" } },
          },
        ],
      }),
    ]);
    if (!recovered) throw new Error("durable turn should produce a recovered session");

    const hydrated = mergeHydratedSessions(
      {
        version: 1,
        sessions: [original, other],
        currentSessionId: other.id,
        activeSessions: [other.id],
        sessionStatusMap: {},
        pendingDeleteRequest: null,
        pendingClearRequest: null,
      },
      [recovered],
    );
    const switchedBack = { ...hydrated, currentSessionId: original.id };
    const assistant = switchedBack.sessions
      .find((session) => session.id === switchedBack.currentSessionId)
      ?.messages.find((message) => message.role === "assistant");

    expect(assistant?.toolCalls).toEqual([
      expect.objectContaining({ id: "call-1", name: "run_command", status: "done" }),
    ]);
    expect(assistant?.content).toBe("Shipped — the release is live.");
    expect(assistant?.parts?.some((part) => part.kind === "tools")).toBe(true);
    expect(assistant?.parts?.slice(-1)[0]).toMatchObject({
      kind: "text",
      text: "Shipped — the release is live.",
    });
  });

  it("repairs a stale non-empty switched-back tab from the durable answer (#3340)", () => {
    const staleOriginal = {
      id: "chat-original",
      title: "Ship the release",
      messages: [
        { id: "local-user", role: "user", content: "Ship the release", status: "done" },
        {
          id: "local-assistant",
          role: "assistant",
          content: "The release is live.",
          status: "done",
          taskId: "task-1",
          toolCalls: [{ id: "call-1", name: "run_command", input: "make release", output: "done", status: "done" }],
          parts: [{ kind: "tools", ids: ["call-1"] }],
        },
      ],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    const other = {
      id: "chat-other",
      title: "Other agent",
      messages: [{ id: "other-user", role: "user", content: "meanwhile", status: "done" }],
      createdAt: 2,
      updatedAt: 2,
    } as ChatSession;
    const recovered = sessionFromDurableTurns(summary(staleOriginal.id), [
      turn({
        text: "The release is live.",
        artifacts: [{ parts: [{ kind: "data", text: "The release is live.", data: { ok: true } }] }],
        history: [
          { role: "ROLE_USER", parts: [{ text: "Ship the release" }] },
          {
            role: "ROLE_AGENT",
            parts: [],
            metadata: { [TOOL]: { toolCallId: "call-1", name: "run_command", phase: "started", args: "make release" } },
          },
          {
            role: "ROLE_AGENT",
            parts: [],
            metadata: { [TOOL]: { toolCallId: "call-1", name: "run_command", phase: "completed", result: "done" } },
          },
        ],
      }),
    ]);
    if (!recovered) throw new Error("durable turn should produce a recovered session");

    const hydrated = mergeHydratedSessions(
      {
        version: 1,
        sessions: [staleOriginal, other],
        currentSessionId: other.id,
        activeSessions: [other.id],
        sessionStatusMap: {},
        pendingDeleteRequest: null,
        pendingClearRequest: null,
      },
      [recovered],
    );
    const switchedBack = { ...hydrated, currentSessionId: staleOriginal.id };
    const assistant = switchedBack.sessions
      .find((session) => session.id === switchedBack.currentSessionId)
      ?.messages.find((message) => message.id === "local-assistant");

    expect(assistant?.toolCalls).toEqual([
      expect.objectContaining({ id: "call-1", name: "run_command", status: "done" }),
    ]);
    expect(assistant?.parts?.some((part) => part.kind === "tools")).toBe(true);
    expect(assistant?.parts?.slice(-1)[0]).toMatchObject({ kind: "text", text: "The release is live." });
  });

  it("repairs switched-back component output without dropping the component (#3340)", () => {
    const staleOriginal = {
      id: "chat-original",
      title: "Release status",
      messages: [
        { id: "local-user", role: "user", content: "show release status", status: "done" },
        {
          id: "local-assistant",
          role: "assistant",
          content: "Here is the latest release.",
          status: "done",
          taskId: "task-1",
          components: [{ component: "key-value", props: { version: "1.2.3" } }],
          parts: [{ kind: "component", spec: { component: "key-value", props: { version: "1.2.3" } } }],
        },
      ],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    const recovered = sessionFromDurableTurns(summary(staleOriginal.id), [
      turn({
        text: "Here is the latest release.",
        artifacts: [{ parts: [{ kind: "data", text: "Here is the latest release.", data: {} }] }],
        history: [
          { role: "ROLE_USER", parts: [{ text: "show release status" }] },
          {
            role: "ROLE_AGENT",
            parts: [{ data: { component: "key-value", props: { version: "1.2.3" } }, metadata: { mimeType: COMPONENT } }],
          },
        ],
      }),
    ]);
    if (!recovered) throw new Error("durable turn should produce a recovered session");

    const hydrated = mergeHydratedSessions(
      {
        version: 1,
        sessions: [staleOriginal],
        currentSessionId: staleOriginal.id,
        activeSessions: [staleOriginal.id],
        sessionStatusMap: {},
        pendingDeleteRequest: null,
        pendingClearRequest: null,
      },
      [recovered],
    );
    const assistant = hydrated.sessions[0].messages.find((message) => message.id === "local-assistant");

    expect(assistant?.components).toEqual([{ component: "key-value", props: { version: "1.2.3" } }]);
    expect(assistant?.parts?.some((part) => part.kind === "component")).toBe(true);
    expect(assistant?.parts?.slice(-1)[0]).toMatchObject({ kind: "text", text: "Here is the latest release." });
  });

  it("flags a session whose EARLIER turn lost its prose even when the last reply is healthy (#3340)", () => {
    // The eligibility gate must scan every assistant turn, not just the last one.
    // Here the tail reply rendered fine, but an earlier tool turn kept only its
    // cards. A last-assistant-only check reads the healthy tail and reports the
    // session as up to date, so the earlier turn's answer stays stripped forever.
    const mixed = {
      id: "chat-mixed",
      title: "Releases",
      messages: [
        { id: "u1", role: "user", content: "Ship v1", status: "done" },
        {
          id: "a1",
          role: "assistant",
          content: "v1 is live.",
          status: "done",
          taskId: "task-a",
          toolCalls: [{ id: "call-1", name: "run_command", status: "done" }],
          parts: [{ kind: "tools", ids: ["call-1"] }],
        },
        { id: "u2", role: "user", content: "Ship v2", status: "done" },
        {
          id: "a2",
          role: "assistant",
          content: "v2 is live.",
          status: "done",
          taskId: "task-b",
          parts: [{ kind: "text", text: "v2 is live." }],
        },
      ],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    expect(needsDurableHydration(mixed)).toBe(true);
  });

  it("repairs a stale EARLIER turn on switch-back and leaves the healthy later reply intact (#3340)", () => {
    const staleEarlier = {
      id: "chat-original",
      title: "Releases",
      messages: [
        { id: "u1", role: "user", content: "Ship v1", status: "done" },
        {
          id: "a1",
          role: "assistant",
          content: "v1 is live.",
          status: "done",
          taskId: "task-a",
          toolCalls: [{ id: "call-1", name: "run_command", status: "done" }],
          parts: [{ kind: "tools", ids: ["call-1"] }],
        },
        { id: "u2", role: "user", content: "Ship v2", status: "done" },
        {
          id: "a2",
          role: "assistant",
          content: "v2 is live.",
          status: "done",
          taskId: "task-b",
          parts: [{ kind: "text", text: "v2 is live." }],
        },
      ],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    const recovered = {
      id: staleEarlier.id,
      title: "Releases",
      messages: [
        { id: "durable-task-a-assistant", role: "assistant", content: "v1 is live.", status: "done", taskId: "task-a" },
        { id: "durable-task-b-assistant", role: "assistant", content: "v2 is live.", status: "done", taskId: "task-b" },
      ],
      createdAt: 1,
      updatedAt: 2,
    } as ChatSession;

    const hydrated = mergeHydratedSessions(
      {
        version: 1,
        sessions: [staleEarlier],
        currentSessionId: staleEarlier.id,
        activeSessions: [staleEarlier.id],
        sessionStatusMap: {},
        pendingDeleteRequest: null,
        pendingClearRequest: null,
      },
      [recovered],
    );
    const messages = hydrated.sessions[0].messages;
    const earlier = messages.find((message) => message.id === "a1");
    const later = messages.find((message) => message.id === "a2");
    // Earlier turn: tool card kept, prose reconciled in as the trailing run.
    expect(earlier?.parts?.some((part) => part.kind === "tools")).toBe(true);
    expect(earlier?.parts?.slice(-1)[0]).toMatchObject({ kind: "text", text: "v1 is live." });
    // Later turn was already whole — untouched.
    expect(later?.parts).toEqual([{ kind: "text", text: "v2 is live." }]);
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

  it("fetches stale non-empty sessions that need durable render repair (#3340)", async () => {
    const ordinary = {
      id: "chat-local",
      title: "Local",
      messages: [{ role: "user", content: "local" }],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    const stale = {
      id: "chat-stale",
      title: "Release",
      messages: [
        { id: "u1", role: "user", content: "Ship it", status: "done" },
        {
          id: "a1",
          role: "assistant",
          content: "Done.",
          status: "done",
          taskId: "task-chat-stale",
          toolCalls: [{ id: "call-1", name: "run_command", status: "done" }],
          parts: [{ kind: "tools", ids: ["call-1"] }],
        },
      ],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    const empty = { id: "chat-empty", title: "Empty", messages: [], createdAt: 1, updatedAt: 1 } as ChatSession;
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ sessions: [ordinary, stale, empty] } as never);
    vi.spyOn(chatStore, "captureHydrationEligibility").mockImplementation((id) => {
      if (id === ordinary.id) return null;
      const localSession = id === stale.id ? stale : id === empty.id ? empty : null;
      return { sessionId: id, localSession };
    });
    const commit = vi.spyOn(chatStore, "hydrateSessions").mockImplementation(() => {});
    vi.spyOn(api, "chatSessions").mockResolvedValue({
      sessions: [summary(ordinary.id), summary(stale.id), summary(empty.id), summary("chat-new")],
    });
    const reads = vi.spyOn(api, "chatSessionTurns").mockImplementation(async (id) => ({
      turns: [turn({ task_id: id === stale.id ? "task-chat-stale" : `task-${id}` })],
    }));

    await hydrateDurableChatSessions();

    expect(reads.mock.calls.map(([id]) => id).sort()).toEqual(["chat-empty", "chat-new", "chat-stale"]);
    expect(commit).toHaveBeenCalledTimes(1);
  });

  it("fetches a session whose earlier turn needs repair even when its last reply is healthy (#3340)", async () => {
    // Regression for the last-assistant-only eligibility gate: the tail reply is
    // whole, an earlier tool turn is not. The session must still be fetched so the
    // earlier turn's answer can be reconciled; a tail-only check would skip it.
    const mixed = {
      id: "chat-mixed",
      title: "Releases",
      messages: [
        { id: "u1", role: "user", content: "Ship v1", status: "done" },
        {
          id: "a1",
          role: "assistant",
          content: "v1 is live.",
          status: "done",
          taskId: "task-mixed-a",
          toolCalls: [{ id: "call-1", name: "run_command", status: "done" }],
          parts: [{ kind: "tools", ids: ["call-1"] }],
        },
        { id: "u2", role: "user", content: "Ship v2", status: "done" },
        {
          id: "a2",
          role: "assistant",
          content: "v2 is live.",
          status: "done",
          taskId: "task-mixed-b",
          parts: [{ kind: "text", text: "v2 is live." }],
        },
      ],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ sessions: [mixed] } as never);
    vi.spyOn(chatStore, "captureHydrationEligibility").mockImplementation((id) =>
      id === mixed.id ? { sessionId: id, localSession: mixed } : null,
    );
    const commit = vi.spyOn(chatStore, "hydrateSessions").mockImplementation(() => {});
    vi.spyOn(api, "chatSessions").mockResolvedValue({ sessions: [summary(mixed.id)] });
    const reads = vi.spyOn(api, "chatSessionTurns").mockResolvedValue({
      turns: [turn({ task_id: "task-mixed-a" })],
    });

    await hydrateDurableChatSessions();

    expect(reads.mock.calls.map(([id]) => id)).toEqual([mixed.id]);
    expect(commit).toHaveBeenCalledTimes(1);
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
