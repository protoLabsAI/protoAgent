// How a `chat.resumed` push renders — extracted from ChatResumeWatch so the branching is
// unit-testable without mounting the component (the streamWatchdog precedent, #1982).
//
// The event is the operator's ONLY live view of a server-fired turn: scheduled fires, watch
// reactions and background push-resumes hold the connection open server-side and never
// stream to the browser, so whatever this builds is all the chat will ever show for them.
// That makes the failed case load-bearing — a crashed turn used to arrive carrying only its
// partial narration, rendering as an ordinary bubble that trailed off mid-sentence, while
// the reason sat unread in the task's terminal `status.message`.

import { isLiveServerTurn, liveMessageId } from "../chat/server-turn-store";
import { applyCanonicalTurnText, settleTurnBubbles, turnBubbleIndexes } from "../chat/turnText";
import type { ChatMessage, ChatPart } from "../lib/types";

export type ResumedTurnEvent = {
  session_id?: unknown;
  text?: unknown;
  task_id?: unknown;
  state?: unknown;
  error?: unknown;
  /** The turn's trigger origin ("scheduler" / "watch-<id>" / "background-resume" / …), stamped
   *  by the server (#3028) so the settled message can render as a compact result card. "" on an
   *  older backend, where ChatResumeWatch falls back to the server-turn store's remembered origin. */
  origin?: unknown;
};

export type ResumedTurnRender = {
  session: string;
  taskId: string;
  /** Trigger origin for the settled message's compact result-card treatment (#3028) — "" when
   *  the server didn't stamp it (ChatResumeWatch then falls back to the store's remembered origin). */
  origin: string;
  /** Dedup key for the ADR 0039 ring-buffer replay on reconnect. */
  key: string;
  failed: boolean;
  /** Markdown for the assistant bubble. */
  content: string;
  status: "done" | "error";
  toast: { tone: "info" | "error"; title: string; message: string };
  /** Title + body for the background/OS notification. */
  notify: { title: string; body: string };
};

/**
 * Build the render for a `chat.resumed` push, or null when there is nothing to show.
 *
 * Null only for a genuinely empty event — no session, or neither text nor error. A failure
 * qualifies on its error ALONE: a turn that died before saying anything is exactly the case
 * that used to disappear, and it is the one most worth seeing.
 */
export function resumedTurnRender(data: ResumedTurnEvent): ResumedTurnRender | null {
  const session = String(data.session_id ?? "");
  const text = String(data.text ?? "");
  const taskId = String(data.task_id ?? "");
  const origin = String(data.origin ?? "");
  // `||`, not `??`: an EMPTY state must fall back too. `??` would leave "" in place, and
  // "" !== "completed" reads as failed — so a payload from a publisher that sets the key
  // but not a value would put a false "Turn failed" on a turn that went fine. A signal
  // that cries wolf is worse than no signal, and the server already resolves it the same
  // way (`str(... or "completed")`).
  const state = String(data.state || "completed");
  const error = String(data.error ?? "");
  const failed = state !== "completed";

  if (!session || (!text && !error)) return null;

  const content = failed && error ? (text ? `${text}\n\n---\n\n**Turn failed:** ${error}` : `**Turn failed:** ${error}`) : text;

  return {
    session,
    taskId,
    origin,
    key: taskId || `${session}:${(text || error).slice(0, 32)}`,
    failed,
    content,
    // Matches ChatSurface's own `failed ? "error" : "done"` so a pushed failure and a
    // streamed one park the bubble identically.
    status: failed ? "error" : "done",
    toast: failed
      ? { tone: "error", title: "Task failed", message: error || "A server-fired turn ended without finishing." }
      : { tone: "info", title: "Task resumed", message: "A waited task picked back up in this chat." },
    notify: {
      title: failed ? "Task failed" : "Task resumed",
      body: (failed ? error || content : text).slice(0, 80),
    },
  };
}

/**
 * Whether the live view's streamed text IS the settled answer, so the settled message can keep
 * the live `parts` — and with them the order the reader was reading (narration, the tool card
 * it ran, more narration). Otherwise the settled message falls back to the grouped layout
 * (tools above all the text), which re-flows the bubble under the reader at the worst moment.
 *
 * Compared with all whitespace removed: the live preview splits text at tool boundaries and
 * the durable answer joins those segments with paragraph breaks (#3210), so spacing differs
 * while the words don't. Any real difference — a terminal replace, a failure note appended —
 * returns false and the authoritative `content` wins, exactly as before.
 */
export function streamedTextIsFinal(parts: ChatPart[] | undefined, content: string): boolean {
  if (!parts?.length) return false;
  const squash = (s: string) => s.replace(/\s+/g, "");
  const streamed = squash(parts.map((p) => (p.kind === "text" ? p.text : "")).join(""));
  return streamed !== "" && streamed === squash(content);
}

/**
 * Land a `chat.resumed` render in a session's transcript, returning the new list.
 *
 * The live preview (#2361) is REPLACED by the authoritative answer — or, with no preview, the
 * answer is appended as `newId`. `origin` tags the settled message, which decides whether it
 * renders as a compact result card or stays a chat message (#3028 / #3443).
 *
 * Either way the settled bubble keeps the live `parts` when the streamed text IS the answer
 * (#3443): that is what stops the text re-flowing under someone mid-read.
 *
 * A preview an operator interjected into was SPLIT at the boundary the agent read that message
 * (the bus's steer-consumed frame): what it had said so far froze above the operator's bubble
 * and the rest streamed into the continuation below. The terminal text is the WHOLE turn, so
 * wholesale-replacing the continuation would print the frozen half's words a second time.
 * Distribute it across the turn instead (turnText.ts) — the frozen half keeps its words, the
 * continuation takes the remainder — and give every bubble of the turn the same origin tag, so
 * the whole report renders as one kind of thing with the operator's message inside it. The
 * continuation keeps its own streamed parts under the same #3443 rule, applied to the
 * REMAINDER it was handed; a split must not re-flow what an un-split turn now leaves alone.
 */
export function settleResumedTurn(
  messages: ChatMessage[],
  render: ResumedTurnRender,
  origin: string | undefined,
  newId: string,
): ChatMessage[] {
  const liveId = liveMessageId(render.taskId, render.session);
  const turn = turnBubbleIndexes(messages, liveId);
  if (turn.length > 1) {
    const streamedParts = messages.find((message) => message.id === liveId)?.parts;
    const landed = applyCanonicalTurnText(messages, liveId, render.content).map((message) => {
      if (message.id === liveId) {
        return {
          ...message,
          status: render.status,
          taskId: render.taskId || message.taskId,
          origin,
          parts: streamedTextIsFinal(streamedParts, message.content) ? streamedParts : undefined,
        };
      }
      return message.splitOf === liveId ? { ...message, origin } : message;
    });
    return settleTurnBubbles(landed, liveId);
  }
  const liveIdx = messages.findIndex((m) => isLiveServerTurn(m, render.taskId, render.session));
  const live = liveIdx >= 0 ? messages[liveIdx] : null;
  const msg: ChatMessage = {
    id: live?.id ?? newId,
    role: "assistant",
    content: render.content,
    createdAt: live?.createdAt ?? Date.now(),
    status: render.status,
    taskId: render.taskId || undefined,
    origin,
    // Keep the tool cards the live view already rendered — the resume payload carries
    // the final TEXT only, so dropping these would erase the turn's visible work.
    toolCalls: live?.toolCalls,
    // `parts` interleaves the STREAMED text, so it is kept only when that text is the
    // settled answer: then the message stays exactly as the reader was reading it. When
    // they differ, the authoritative `content` supersedes it and the message falls back
    // to the grouped tools→content layout history-loaded messages already use.
    ...(live && streamedTextIsFinal(live.parts, render.content) ? { parts: live.parts } : {}),
    // No preview of this turn to replace, so the answer lands as its own row, possibly
    // after another turn that is still running here. Marked so it never stands in for that
    // turn's lead row (see ChatMessage.outOfBand).
    ...(live ? {} : { outOfBand: true }),
  };
  return liveIdx >= 0 ? messages.map((m, i) => (i === liveIdx ? msg : m)) : [...messages, msg];
}
