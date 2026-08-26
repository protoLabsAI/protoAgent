/** Inserting a room bubble (a delegate's reply, or the lead's ask) into a streaming
 *  transcript at the point it happened (#3042).
 *
 *  The lead's whole turn is ONE streaming message (id = the assistant placeholder):
 *  text, tool cards, more text, all in order. A `delegate_to` fires *mid-stream*, so its
 *  bubble must land between what the lead had said and what it says next — not floated
 *  above the whole message (the bug: the reply rendered above "I'll coordinate…" and the
 *  list_agents card). This splits the placeholder at the delegation point: freeze what
 *  it holds so far as a completed lead bubble, drop the room bubble after it, and leave a
 *  fresh empty placeholder (same id) for the lead's continued streaming.
 */

import type { ChatMessage } from "../lib/types";

export function isEmptyPlaceholder(m: ChatMessage | undefined): boolean {
  return !m?.content && !m?.parts?.length && !m?.toolCalls?.length && !m?.reasoning;
}

/** Return the messages array with `bubble` inserted at the live delegation point.
 *
 *  - Placeholder empty (a `@x @y` fan-out, or the lead delegated before saying anything):
 *    insert the bubble BEFORE the still-empty placeholder — the placeholder stays last,
 *    where the streaming indicator lives, and the lead's future text streams in after.
 *  - Placeholder non-empty (the lead streamed work, THEN delegated): split — freeze the
 *    placeholder's current content as a done lead bubble, insert the room bubble after it,
 *    and reset the placeholder (same id) to empty so continued streaming lands after.
 *
 *  `newId` is the id minted for the frozen lead bubble; kept a parameter so the caller
 *  owns id generation (and tests are deterministic).
 */
export function insertConversationBubbles(
  messages: ChatMessage[],
  assistantId: string,
  bubbles: ChatMessage[],
  newId: string,
): ChatMessage[] {
  if (!bubbles.length) return messages;
  const at = messages.findIndex((m) => m.id === assistantId);
  if (at === -1) return [...messages, ...bubbles];
  const placeholder = messages[at];
  if (isEmptyPlaceholder(placeholder)) {
    return [...messages.slice(0, at), ...bubbles, ...messages.slice(at)];
  }
  const frozen: ChatMessage = { ...placeholder, id: newId, status: "done" };
  const reset: ChatMessage = {
    id: assistantId,
    role: "assistant",
    content: "",
    parts: [],
    createdAt: Date.now(),
    status: "streaming",
    taskId: placeholder.taskId,
  };
  return [...messages.slice(0, at), frozen, ...bubbles, reset, ...messages.slice(at + 1)];
}

/** Backward-compatible single-bubble form used by addressed room replies. */
export function insertRoomBubble(
  messages: ChatMessage[],
  assistantId: string,
  bubble: ChatMessage,
  newId: string,
): ChatMessage[] {
  return insertConversationBubbles(messages, assistantId, [bubble], newId);
}
