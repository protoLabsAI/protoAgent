// Live view of a SERVER-FIRED turn (#2361) — extracted from the watcher so the reducer is
// unit-testable without mounting anything (the resumedTurn / streamWatchdog precedent).
//
// Scheduled fires, watch reactions and background push-resumes run by self-POSTing into a
// chat session. The server holds that A2A stream, so the browser — which only renders turns
// IT streamed — showed a typing indicator and nothing else for the whole turn. #1767 gave
// them the indicator; the backend now republishes their frames as `chat.progress`, and this
// folds those frames into a growing assistant message so the operator can watch the work.
//
// The message is DISPLAY-ONLY, exactly like the one ChatResumeWatch appends: the backend
// owns conversation history, so nothing here is ever fed back to the model. It is a live
// preview that `chat.resumed` then replaces with the authoritative final answer.
//
// It carries `taskId` and a stable `id` deliberately, not just for the replace: those are
// what ChatSurface's self-heal keys on. If the turn dies without a terminal event, the
// preview would otherwise sit at `streaming` forever — instead the next mount reconciles it
// against the durable task via `tasks/get` and settles it. A live view that can strand a
// bubble is worse than no live view, so this failure mode is covered by construction.

import type { ChatMessage, ChatPart, ToolCall } from "../lib/types";

export type ChatProgressEvent = {
  session_id?: unknown;
  task_id?: unknown;
  phase?: unknown;
  text?: unknown;
  tool?: unknown;
  tool_call_id?: unknown;
  output?: unknown;
  error?: unknown;
};

/** A parsed frame, or null when the event is malformed / not for a session we track. */
export type ProgressFrame =
  | { session: string; taskId: string; kind: "text"; text: string }
  | {
      session: string;
      taskId: string;
      kind: "tool";
      done: boolean;
      id: string;
      name: string;
      output: string;
      error: boolean;
    };

export function parseProgress(data: ChatProgressEvent): ProgressFrame | null {
  const session = String(data.session_id ?? "");
  const taskId = String(data.task_id ?? "");
  if (!session) return null;
  const phase = String(data.phase ?? "");
  if (phase === "text") {
    const text = String(data.text ?? "");
    return text ? { session, taskId, kind: "text", text } : null;
  }
  if (phase === "tool_start" || phase === "tool_end") {
    // A tool frame without an id can't be correlated start→end, so it would render as a
    // duplicate card on completion. Drop it rather than show the work twice.
    const id = String(data.tool_call_id ?? "");
    if (!id) return null;
    return {
      session,
      taskId,
      kind: "tool",
      done: phase === "tool_end",
      id,
      name: String(data.tool ?? ""),
      output: String(data.output ?? ""),
      error: Boolean(data.error),
    };
  }
  return null;
}

/** The id given to a session's live server-turn message. Deterministic, so a frame arriving
 *  after a re-render still finds the same bubble, and so the resume can replace it. */
export function liveMessageId(taskId: string, session: string): string {
  return `server-turn-${taskId || session}`;
}

/** True when `msg` is the live preview for this server-fired turn — the message
 *  `chat.resumed` should REPLACE rather than append a second bubble beside. */
export function isLiveServerTurn(msg: ChatMessage, taskId: string, session: string): boolean {
  return !!msg.id && msg.id === liveMessageId(taskId, session);
}

/**
 * Fold one frame into a session's message list, returning the new list.
 *
 * Appends a fresh live message the first time, then mutates it in place on later frames.
 * Text accumulates into a trailing text part; a tool start opens a card and its matching
 * end completes the SAME card (correlated by tool_call_id), so a finished tool doesn't
 * render twice. Parts stay in frame-arrival order, which is what makes narration read
 * above the tool it preceded — the same ordering contract the live stream path keeps.
 */
export function applyProgressFrame(messages: ChatMessage[], frame: ProgressFrame): ChatMessage[] {
  const id = liveMessageId(frame.taskId, frame.session);
  const idx = messages.findIndex((m) => m.id === id);
  const base: ChatMessage =
    idx >= 0
      ? messages[idx]
      : {
          id,
          role: "assistant",
          content: "",
          parts: [],
          toolCalls: [],
          createdAt: Date.now(),
          status: "streaming",
          taskId: frame.taskId || undefined,
        };

  const parts: ChatPart[] = [...(base.parts ?? [])];
  const toolCalls: ToolCall[] = [...(base.toolCalls ?? [])];
  let content = base.content;

  if (frame.kind === "text") {
    content += frame.text;
    const last = parts[parts.length - 1];
    if (last && last.kind === "text") parts[parts.length - 1] = { kind: "text", text: last.text + frame.text };
    else parts.push({ kind: "text", text: frame.text });
  } else {
    const existing = toolCalls.findIndex((t) => t.id === frame.id);
    const call: ToolCall = {
      id: frame.id,
      name: frame.name || (existing >= 0 ? toolCalls[existing].name : ""),
      input: existing >= 0 ? toolCalls[existing].input : "",
      output: frame.output || (existing >= 0 ? toolCalls[existing].output : ""),
      status: frame.error ? "error" : frame.done ? "done" : "running",
    };
    if (existing >= 0) {
      toolCalls[existing] = call;
    } else {
      toolCalls.push(call);
      const last = parts[parts.length - 1];
      // Extend the trailing tool group rather than opening a new one per call, so a run
      // of back-to-back tools renders as one block like a normal streamed turn.
      if (last && last.kind === "tools") parts[parts.length - 1] = { kind: "tools", ids: [...last.ids, frame.id] };
      else parts.push({ kind: "tools", ids: [frame.id] });
    }
  }

  const next: ChatMessage = { ...base, content, parts, toolCalls, status: "streaming" };
  if (idx >= 0) {
    const out = [...messages];
    out[idx] = next;
    return out;
  }
  return [...messages, next];
}
