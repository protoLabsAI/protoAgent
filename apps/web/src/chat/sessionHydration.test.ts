import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type DurableChatSession, type DurableChatTurn } from "../lib/api";
import {
  chatStore,
  DEFAULT_SESSION_TITLE,
  mergeHydratedSessions,
  needsDurableHydration,
  type ChatSession,
} from "./chat-store";
import type { ChatMessage } from "../lib/types";
import {
  HYDRATION_CONCURRENCY,
  hydrateDurableChatSessions,
  messagesFromDurableTurn,
  sessionFromDurableTurns,
} from "./sessionHydration";
import { applyText, applyToolEvent } from "./turnReducers";
import { applyCanonicalTurnText } from "./turnText";

const TOOL = "https://proto-labs.ai/a2a/ext/tool-call-v1";
const COST = "https://proto-labs.ai/a2a/ext/cost-v1";
const REASONING = "application/vnd.protolabs.reasoning-v1+json";
const COMPONENT = "application/vnd.protolabs.component-v1+json";
const CONTEXT = "application/vnd.protolabs.context-v1+json";
const STEER = "application/vnd.protolabs.steer-consumed-v1+json";

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

  it("rebuilds a turn stored before the server kept prompts — answer and interjection, no prompt", () => {
    // The exact row shape the v0.164.0 hub returned (QA 2026-09-11): agent frames only —
    // tool-call metadata on part-less messages and a steer marker — and no ROLE_USER.
    // Those rows age out with the task store's retention; until then they must still
    // render the answer, its tool cards and the interjection the agent read.
    const legacy = turn({
      text: "The workspace root contained two entries.",
      artifacts: [{ parts: [{ text: "The workspace root contained two entries." }] }],
      history: [
        {
          role: "ROLE_AGENT",
          metadata: { [TOOL]: { toolCallId: "call-1", name: "list_dir", phase: "started", args: "" } },
        },
        {
          role: "ROLE_AGENT",
          metadata: { [TOOL]: { toolCallId: "call-1", name: "list_dir", phase: "completed", result: "AGENTS.md" } },
        },
        {
          role: "ROLE_AGENT",
          parts: [{ data: { items: [{ id: "msg-1", text: "Also count them." }] }, metadata: { mimeType: STEER } }],
        },
      ],
    });
    const session = sessionFromDurableTurns(summary(), [legacy]);
    expect(session?.messages.map((message) => [message.role, message.content])).toEqual([
      ["assistant", ""],
      ["user", "Also count them."],
      ["assistant", "The workspace root contained two entries."],
    ]);
    expect(session?.messages[0]).toMatchObject({
      status: "done",
      toolCalls: [{ id: "call-1", name: "list_dir", status: "done" }],
    });
    // No prompt survives in such a row, so the tab is named after what does.
    expect(session?.title).toBe("Also count them.");
  });

  it("rebuilds a mid-turn interjection as a user message where the agent read it", () => {
    // A steered turn: the agent worked, the operator interjected, the agent read it and
    // carried on. #3446 settles that into the LIVE transcript as a user bubble at the
    // split; a rebuilt transcript has to agree, or the interjection vanishes on a new
    // device. The flattened answer lands on the trailing bubble — the durable artifacts
    // cannot say how much of the prose came before the interjection.
    const steered = turn({
      task_id: "task-steered",
      text: "Two entries, and the time is noon.",
      artifacts: [{ parts: [{ text: "Two entries, and the time is noon." }] }],
      history: [
        { role: "ROLE_USER", parts: [{ text: "List the workspace" }] },
        {
          role: "ROLE_AGENT",
          metadata: { [TOOL]: { toolCallId: "call-1", name: "list_dir", phase: "completed", result: "AGENTS.md" } },
        },
        {
          role: "ROLE_AGENT",
          parts: [{ data: { items: [{ id: "msg-steer-1", text: "Also tell me the time." }] }, metadata: { mimeType: STEER } }],
        },
        {
          role: "ROLE_AGENT",
          metadata: { [TOOL]: { toolCallId: "call-2", name: "current_time", phase: "completed", result: "12:00" } },
        },
      ],
    });
    const messages = messagesFromDurableTurn(steered);
    expect(messages.map((message) => [message.id, message.role, message.content])).toEqual([
      ["durable-task-steered-user", "user", "List the workspace"],
      ["durable-task-steered-assistant-0", "assistant", ""],
      ["msg-steer-1", "user", "Also tell me the time."], // the steer's own id — a later live settle is a no-op
      ["durable-task-steered-assistant", "assistant", "Two entries, and the time is noon."],
    ]);
    // The halves are one turn: the frozen one names the trailing bubble (turnText.ts).
    expect(messages[1]).toMatchObject({ splitOf: "durable-task-steered-assistant", status: "done" });
    // Work done before the interjection stays above it; work after it, below.
    expect(messages[1].toolCalls?.map((call) => call.id)).toEqual(["call-1"]);
    expect(messages[3].toolCalls?.map((call) => call.id)).toEqual(["call-2"]);
  });

  it("keeps several interjections in the order the agent read them", () => {
    const messages = messagesFromDurableTurn(
      turn({
        task_id: "task-two",
        history: [
          { role: "ROLE_USER", parts: [{ text: "Start" }] },
          { role: "ROLE_AGENT", metadata: { [TOOL]: { toolCallId: "c1", name: "a", phase: "completed", result: "ok" } } },
          { role: "ROLE_AGENT", parts: [{ data: { items: [{ id: "s1", text: "first aside" }] }, metadata: { mimeType: STEER } }] },
          { role: "ROLE_AGENT", metadata: { [TOOL]: { toolCallId: "c2", name: "b", phase: "completed", result: "ok" } } },
          { role: "ROLE_AGENT", parts: [{ data: { items: [{ id: "s2", text: "second aside" }] }, metadata: { mimeType: STEER } }] },
        ],
      }),
    );
    expect(messages.map((message) => message.content)).toEqual([
      "Start",
      "",
      "first aside",
      "",
      "second aside",
      "answer",
    ]);
  });

  it("adds no blank bubble around an interjection that opened or closed the turn", () => {
    // Read before the agent did anything: nothing to freeze above it. And when the agent
    // says nothing after the last one, the trailing bubble is dropped rather than settled
    // as a blank row (the live path's settleTurnBubbles folds the same case).
    const opening = messagesFromDurableTurn(
      turn({
        task_id: "task-open",
        history: [
          { role: "ROLE_USER", parts: [{ text: "Go" }] },
          { role: "ROLE_AGENT", parts: [{ data: { items: [{ id: "s1", text: "wait — also this" }] }, metadata: { mimeType: STEER } }] },
        ],
      }),
    );
    expect(opening.map((message) => [message.role, message.content])).toEqual([
      ["user", "Go"],
      ["user", "wait — also this"],
      ["assistant", "answer"],
    ]);

    const closing = messagesFromDurableTurn(
      turn({
        task_id: "task-close",
        text: "",
        artifacts: [],
        history: [
          { role: "ROLE_USER", parts: [{ text: "Go" }] },
          { role: "ROLE_AGENT", metadata: { [TOOL]: { toolCallId: "c1", name: "a", phase: "completed", result: "ok" } } },
          { role: "ROLE_AGENT", parts: [{ data: { items: [{ id: "s2", text: "never mind" }] }, metadata: { mimeType: STEER } }] },
        ],
      }),
    );
    expect(closing.map((message) => [message.role, message.content])).toEqual([
      ["user", "Go"],
      ["assistant", ""],
      ["user", "never mind"],
    ]);
    expect(closing[1].toolCalls?.map((call) => call.id)).toEqual(["c1"]);
  });

  it("draws no operator bubble for a hidden send and titles the tab from the first visible prompt", () => {
    // A dismissal/approval resume, a regenerate or a goal kickoff is sent `hidden`: the
    // live transcript never showed it, so the rebuilt one must not invent it — least of
    // all as the tab's title.
    const dismissed = turn({
      task_id: "task-dismissed",
      history: [{
        role: "ROLE_USER",
        parts: [{ text: "[dismissed] The operator dismissed this request without providing input." }],
        metadata: { hitl_resume: true, hidden: true },
      }],
    });
    const visible = turn({
      task_id: "task-visible",
      last_updated: "2026-08-20T12:01:00Z",
      history: [{ role: "ROLE_USER", parts: [{ text: "Ship the release" }] }],
    });
    const session = sessionFromDurableTurns(summary(), [dismissed, visible]);
    expect(session?.messages.map((message) => message.role)).toEqual(["assistant", "user", "assistant"]);
    expect(session?.messages[1]).toMatchObject({ role: "user", content: "Ship the release" });
    expect(session?.title).toBe("Ship the release");
  });

  it("rebuilds a server-fired turn as its answer, without the machine prompt that fired it", () => {
    const fired = turn({
      history: [{
        role: "ROLE_USER",
        parts: [{ text: "[Autonomous wake — a wait you scheduled has elapsed. Continue:]\n\ncheck the deploy" }],
        metadata: { origin: "scheduler", scheduler_job_id: "job-1" },
      }],
    });
    const messages = messagesFromDurableTurn(fired);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ role: "assistant", content: "answer" });
    expect(sessionFromDurableTurns(summary(), [fired])?.title).toBe(DEFAULT_SESSION_TITLE);
  });

  it("shows an attachment send as the bubble it was, not the document context it carried", () => {
    // The model receives the pipeline context prepended; the bubble only ever showed the
    // typed text + 📎 list ("never a raw doc/data dump"). The console records that bubble.
    const attached = turn({
      history: [{
        role: "ROLE_USER",
        parts: [{ text: "[Attached file: notes.txt]\nline one\nline two\n[end of notes.txt]\n\nSummarize it" }],
        metadata: { display: "Summarize it\n\nAttached: notes.txt" },
      }],
    });
    const [user] = messagesFromDurableTurn(attached);
    expect(user).toMatchObject({ role: "user", content: "Summarize it\n\nAttached: notes.txt" });
    expect(sessionFromDurableTurns(summary(), [attached])?.title).toBe("Summarize it\n\nAttached: notes.txt");
  });

  it("recovers incognito from the newest OPERATOR message, not a later server-fired one", () => {
    // A scheduled fire into a private chat carries no incognito flag of its own; reading
    // it as the newest "user" frame would reopen the recovered tab as ordinary.
    const privateTurn = turn({
      task_id: "task-private",
      history: [{ role: "ROLE_USER", parts: [{ text: "private" }], metadata: { incognito: true } }],
    });
    const scheduled = turn({
      task_id: "task-scheduled",
      last_updated: "2026-08-20T12:05:00Z",
      history: [{ role: "ROLE_USER", parts: [{ text: "[Autonomous wake]" }], metadata: { origin: "scheduler" } }],
    });
    expect(sessionFromDurableTurns(summary(), [privateTurn, scheduled])?.incognito).toBe(true);
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
      serverTurnControls: {},
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
        serverTurnControls: {},
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
        serverTurnControls: {},
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
        serverTurnControls: {},
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
        serverTurnControls: {},
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

  it("never flags — or rewrites — a HEALTHY narrate → tool → narrate turn (#3439 review)", () => {
    // Built from the frames the server streams: the post-tool narration opens with the
    // paragraph break inside its delta, so the settled bubble's flat `content` reads
    // "A\n\nB" while its parts render "A" + "B". Compared byte-for-byte, every such turn
    // looked stripped, so each boot re-downloaded up to 50 sessions and rewrote them.
    let bubble: ChatMessage = { id: "a1", role: "assistant", content: "", createdAt: 1, status: "streaming", taskId: "task-1" };
    bubble = applyText(bubble, "I'll check the time first.", true);
    bubble = applyToolEvent(bubble, { id: "t1", name: "current_time", phase: "start" });
    bubble = applyToolEvent(bubble, { id: "t1", name: "current_time", phase: "end", output: "12:00" });
    bubble = applyText(bubble, "\n\nIt is noon.", true);
    const canonical = "I'll check the time first.\n\nIt is noon.";
    const [, settled] = applyCanonicalTurnText([{ id: "u", role: "user", content: "hi" } as ChatMessage, bubble], "a1", canonical);
    const healthy = {
      id: "chat-healthy",
      title: "hi",
      messages: [{ id: "u", role: "user", content: "hi", status: "done" }, { ...settled, status: "done" }],
      createdAt: 1,
      updatedAt: 1,
    } as ChatSession;
    expect(needsDurableHydration(healthy)).toBe(false);

    // And when a durable read does come back for it, nothing is rewritten.
    const current = {
      version: 1,
      sessions: [healthy],
      currentSessionId: healthy.id,
      activeSessions: [healthy.id],
      sessionStatusMap: {},
      pendingDeleteRequest: null,
      pendingClearRequest: null,
      serverTurnControls: {},
    };
    const recovered = sessionFromDurableTurns(summary(healthy.id), [turn({ text: canonical })]);
    if (!recovered) throw new Error("durable turn should produce a recovered session");
    expect(mergeHydratedSessions(current, [recovered]).sessions[0]).toBe(healthy);
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
