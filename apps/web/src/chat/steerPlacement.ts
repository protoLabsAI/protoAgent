import type { ChatMessage, ConsumedSteer } from "../lib/types";
import { insertConversationBubbles } from "./roomBubble";
import { turnBubbleIndexes } from "./turnText";

function settledBubbles(consumed: ConsumedSteer[], createdAt: number): ChatMessage[] {
  return consumed.map((item, index) => ({
    id: item.id,
    role: "user",
    content: item.text,
    createdAt: createdAt + index,
    status: "done",
  }));
}

/** Settle consumed steers into the visible transcript, idempotently.
 *
 * With a live assistant id, split at the exact streamed boundary. Without one
 * (poll/turn-end compatibility fallback), retain the legacy conservative placement
 * immediately before the current assistant message: the fallback knows the steer
 * shaped that reply, but has no honest finer-grained position. */
export function placeConsumedSteers(
  messages: ChatMessage[],
  consumed: ConsumedSteer[],
  opts: { inlineAssistantId?: string; frozenId: string; createdAt: number },
): ChatMessage[] {
  const existingIds = new Set(messages.map((message) => message.id));
  const fresh = consumed.filter((item) => !existingIds.has(item.id));
  if (!fresh.length) return messages;
  const settled = settledBubbles(fresh, opts.createdAt);
  const live = opts.inlineAssistantId
    ? messages.find((message) => message.id === opts.inlineAssistantId && message.status === "streaming")
    : undefined;
  if (opts.inlineAssistantId && live) {
    return insertConversationBubbles(messages, opts.inlineAssistantId, settled, opts.frozenId);
  }
  const next = [...messages];
  let at = next.length;
  for (let index = next.length - 1; index >= 0; index--) {
    if (next[index].role === "assistant") {
      at = index;
      break;
    }
  }
  next.splice(at, 0, ...settled);
  return next;
}

/** Settle interjections a SERVER-FIRED turn consumed, idempotently.
 *
 * A server turn renders through a bus-fed preview (`liveId` — server-turn-store's
 * `liveMessageId`), not a stream this browser owns, so its anchors differ from
 * `placeConsumedSteers`:
 *
 * - `exact` (the server's steer-consumed frame): split the still-streaming preview at
 *   that boundary, the same cut a browser-owned stream makes. If the turn has shown
 *   nothing yet there is no preview to cut — the interjection is simply the newest row,
 *   and the frames that follow grow the preview beneath it.
 * - not `exact` (turn-end reconcile after a missed frame): the boundary is unknown, so
 *   land conservatively ABOVE the turn's first bubble — it shaped that reply. Splitting
 *   at the preview's current end would claim a position nobody reported.
 *
 * Never "before the last assistant message": when the turn has no bubble yet, that is
 * the PREVIOUS turn's answer, and the operator's message would jump above it. */
export function placeServerTurnSteers(
  messages: ChatMessage[],
  consumed: ConsumedSteer[],
  opts: { liveId: string; exact: boolean; frozenId: string; createdAt: number },
): ChatMessage[] {
  const existingIds = new Set(messages.map((message) => message.id));
  const fresh = consumed.filter((item) => !existingIds.has(item.id));
  if (!fresh.length) return messages;
  const settled = settledBubbles(fresh, opts.createdAt);
  if (opts.exact && messages.some((message) => message.id === opts.liveId && message.status === "streaming")) {
    return insertConversationBubbles(messages, opts.liveId, settled, opts.frozenId);
  }
  const turn = turnBubbleIndexes(messages, opts.liveId);
  if (turn.length) return [...messages.slice(0, turn[0]), ...settled, ...messages.slice(turn[0])];
  return [...messages, ...settled];
}
