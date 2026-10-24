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

import { delegateTimelineMessage } from "../lib/delegateProgress";
import type { ChatMessage, RoomReply } from "../lib/types";

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
  // `splitOf` links the frozen half back to the continuation: one server turn now
  // renders as two bubbles, and everything that lands the turn's canonical text has
  // to see them as one (turnText.ts).
  const frozen: ChatMessage = { ...placeholder, id: newId, status: "done", splitOf: assistantId };
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

/** Does this reply render words the turn's canonical answer text also carries (#3449)?
 *  Then the turn is stamped answered-by-participants and no producer of that text lands
 *  it. Three conditions, all load-bearing:
 *    - a REPLY, not an ask (an ask's text is the question) and not the room's own note
 *      (which is the part of the answer NO bubble carries);
 *    - the server claimed it (`inAnswer`);
 *    - the OPERATOR did the addressing. A reply the LEAD addressed (`delegate_to`,
 *      `from: "assistant"`) can never be the whole of the turn's answer — the lead's
 *      answer is its own synthesis — so honouring a claim there would delete the lead's
 *      words. The mention path is the only producer that sets both, and nothing stops a
 *      fork or plugin from emitting room frames, so this is checked rather than assumed. */
export function replyClaimsAnswer(reply: RoomReply): boolean {
  return Boolean(reply.author) && reply.inAnswer === true && reply.from === "operator";
}

/** The bubble a room frame renders as — a participant's reply, the lead's ask, or the
 *  room's own note — or null when it has no text to show. Shared by the live stream and
 *  the rebuild from durable turns, so a reloaded transcript draws what the live one did. */
export function roomReplyBubble(
  reply: RoomReply,
  id: string,
  opts: { createdAt?: number; turnTaskId?: string } = {},
): ChatMessage | null {
  if (!reply.text) return null;
  return {
    id,
    role: "assistant",
    content: reply.text,
    // A coding delegate's reply in the order it happened — its narration with the tool
    // calls between it — instead of the narration alone, which reads in jumps.
    ...(reply.timeline ? delegateTimelineMessage(reply.timeline, id) : {}),
    ...(opts.createdAt !== undefined ? { createdAt: opts.createdAt } : {}),
    status: "done",
    ...(reply.author ? { author: reply.author } : {}),
    ...(reply.addressedTo ? { addressedTo: reply.addressedTo } : {}),
    ...(reply.delegation ? { delegation: reply.delegation } : {}),
    // Bubbles that carry the turn's own answer carry its task id too, so Copy / Fork /
    // Rewind / View prompt land on the message that actually shows the answer (#3449 C).
    // The turn's other half is a work card with no prose, and the action row needs
    // `content`. Read paths that group a turn by task id all skip `author`-bearing bubbles
    // by design, so this cannot make one an anchor for canonical text — `turnBubbleIndexes`
    // keys on `splitOf`, never on the task.
    ...(replyClaimsAnswer(reply) && opts.turnTaskId ? { taskId: opts.turnTaskId } : {}),
  };
}
