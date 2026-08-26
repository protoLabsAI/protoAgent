import type { ChatMessage, ConsumedSteer } from "../lib/types";
import { insertConversationBubbles } from "./roomBubble";

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
  const settled: ChatMessage[] = fresh.map((item, index) => ({
    id: item.id,
    role: "user",
    content: item.text,
    createdAt: opts.createdAt + index,
    status: "done",
  }));
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
