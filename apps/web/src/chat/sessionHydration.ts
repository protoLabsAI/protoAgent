// Durable chat recovery (#2888, ADR 0104). The server returns the raw A2A
// task wire; user text becomes the prompt bubble and the assistant side runs
// through the same snapshot dispatcher + reducers as live/reattached turns.

import {
  api,
  replayDurableChatTurn,
  textFromParts,
  type DurableChatSession,
  type DurableChatTurn,
} from "../lib/api";
import type { ChatMessage } from "../lib/types";
import { chatStore, DEFAULT_SESSION_TITLE, type ChatSession } from "./chat-store";
import { applyComponent, applyReasoning, applyText, applyToolEvent, applyUsage } from "./turnReducers";

const TERMINAL = /completed|failed|canceled|cancelled|rejected/i;
const FAILED = /failed|canceled|cancelled/i;

function timestamp(value: string | null): number {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function firstUserText(turn: DurableChatTurn): string {
  const user = (turn.history ?? []).find((message) => {
    const role = (message.role ?? "").toLowerCase();
    return role === "user" || role.includes("role_user");
  });
  return textFromParts(user?.parts);
}

function titleFromPrompt(prompt: string): string {
  const text = prompt.trim();
  if (!text) return DEFAULT_SESSION_TITLE;
  return text.length > 52 ? `${text.slice(0, 49)}...` : text;
}

/** Pure conversion of one task into the local prompt/answer pair. */
export function messagesFromDurableTurn(turn: DurableChatTurn): ChatMessage[] {
  const at = timestamp(turn.last_updated);
  const prompt = firstUserText(turn);
  const messages: ChatMessage[] = prompt
    ? [{ id: `durable-${turn.task_id}-user`, role: "user", content: prompt, createdAt: at, status: "done" }]
    : [];
  let assistant: ChatMessage = {
    id: `durable-${turn.task_id}-assistant`,
    role: "assistant",
    content: "",
    createdAt: at,
    status: "streaming",
    taskId: turn.task_id,
  };
  replayDurableChatTurn(turn, "", {
    onText: (text, append) => {
      assistant = applyText(assistant, text, append);
    },
    onReasoning: (delta) => {
      assistant = applyReasoning(assistant, delta);
    },
    onToolCall: (event) => {
      if (event.name !== "show_component") assistant = applyToolEvent(assistant, event);
    },
    onComponent: (spec) => {
      assistant = applyComponent(assistant, spec);
    },
    onCost: (usage) => {
      assistant = applyUsage(assistant, usage);
    },
    onContext: (contextWindow) => {
      assistant = { ...assistant, contextWindow };
    },
  });
  if (TERMINAL.test(turn.state)) {
    assistant = {
      ...assistant,
      status: FAILED.test(turn.state) ? "error" : "done",
      toolCalls: assistant.toolCalls?.map((call) =>
        call.status === "running" ? { ...call, status: "done" as const } : call,
      ),
    };
  }
  return [...messages, assistant];
}

/** Build one fixed-id local session from its ordered durable turns. */
export function sessionFromDurableTurns(
  summary: DurableChatSession,
  turns: DurableChatTurn[],
): ChatSession | null {
  const messages = turns.flatMap(messagesFromDurableTurn);
  if (!messages.length) return null;
  const createdAt = timestamp(turns[0]?.last_updated ?? summary.last_updated);
  const updatedAt = timestamp(summary.last_updated);
  const firstPrompt = messages.find((message) => message.role === "user")?.content ?? "";
  return {
    id: summary.session_id,
    title: titleFromPrompt(firstPrompt),
    messages,
    createdAt,
    updatedAt,
  };
}

export const SESSION_INDEX_LIMIT = 50;
export const SESSION_TURN_LIMIT = 50;
export const HYDRATION_CONCURRENCY = 4;

/** Fetch only server-only or locally empty sessions, with bounded fan-out.
 * Every read is best-effort: an offline/cold fleet member leaves local chat
 * untouched and will be tried again on the next full page boot. */
export async function hydrateDurableChatSessions(): Promise<void> {
  let summaries: DurableChatSession[];
  try {
    summaries = (await api.chatSessions(SESSION_INDEX_LIMIT)).sessions;
  } catch {
    return;
  }
  const local = new Map(chatStore.getSnapshot().sessions.map((session) => [session.id, session]));
  const wanted = summaries.filter((summary) => {
    const session = local.get(summary.session_id);
    return !session || session.messages.length === 0;
  });
  const hydrated: ChatSession[] = [];
  let cursor = 0;
  async function worker() {
    while (cursor < wanted.length) {
      const summary = wanted[cursor++];
      try {
        const { turns } = await api.chatSessionTurns(summary.session_id, SESSION_TURN_LIMIT);
        const session = sessionFromDurableTurns(summary, turns);
        if (session) hydrated.push(session);
      } catch {
        // One session failing must not discard successful siblings.
      }
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(HYDRATION_CONCURRENCY, wanted.length) }, () => worker()),
  );
  if (hydrated.length) chatStore.hydrateSessions(hydrated);
}
