// Local-only dismissal of CANCELLED delegation cards (#3095). Stopping a running `task`
// delegation (Tier 2 — the Stop on its card) settles the card through the normal tool_end
// stream, same as any finished call, so it becomes permanent backend history: a reload
// re-renders it, and unlike a local errored turn (#1695) nothing ever expires it out of
// the transcript. These helpers give exactly that one case a dismiss (×): dismissed ids
// persist in localStorage (same design as the report/scheduled chips' sets in
// ChatMessageView, #2923/#2990) and the cards are filtered out of THIS client's view at
// render time — the backend history is never mutated, so a reload, /export, publish, and
// every other client still hold the full turn.

import type { ChatMessage, ToolCall } from "../lib/types";

/** The sentinel prefix `graph/agent.py`'s `task` tool returns when the operator cancels a
 *  running delegation. The frame also carries `error: true` (the card settles as an X), so
 *  status alone can't tell "cancelled by the user" from a real failure — this prefix is
 *  the only client-visible signal, and it survives reloads (the ToolMessage content IS the
 *  stored result). */
export const CANCELLED_DELEGATION_PREFIX = "[delegation cancelled by the user";

/** True only for the settled card of a delegation the OPERATOR cancelled. Deliberately
 *  narrow — the name must be `task` (the only card that offers Stop), the call must have
 *  settled, and the result must be the cancellation sentinel — so a normal completed call
 *  (real output) or a real failure can never grow a dismiss affordance. */
export function isCancelledDelegation(call: ToolCall): boolean {
  return (
    call.name === "task" &&
    call.status !== "running" &&
    (call.output ?? "").startsWith(CANCELLED_DELEGATION_PREFIX)
  );
}

// localStorage (not sessionStorage) so a reload doesn't resurrect a dismissed card; capped
// like the panel's seen-set (#2692).
const DISMISSED_KEY = "protoagent.chat.dismissedToolCalls";

export function dismissedToolCallSet(): Set<string> {
  try {
    return new Set(JSON.parse(window.localStorage.getItem(DISMISSED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

/** Record a dismissal and return the updated set (a NEW object, safe as React state).
 *  Read-merge-write so a dismiss in another tab sharing this key isn't clobbered. */
export function rememberDismissedToolCall(id: string): Set<string> {
  const s = dismissedToolCallSet();
  s.add(id);
  try {
    window.localStorage.setItem(DISMISSED_KEY, JSON.stringify([...s].slice(-300)));
  } catch {
    /* storage unavailable — the card still hides for this page's lifetime */
  }
  return s;
}

/** The message with its dismissed CANCELLED delegation cards (and their nested subagent
 *  tools) filtered out — or the SAME message object when nothing applies. The guard is
 *  re-checked per call: an id in the set only ever hides a card that still reads as a
 *  cancelled delegation, so a normal completed/failed call can never be filtered out,
 *  whatever the set contains. */
export function hideDismissedToolCalls(message: ChatMessage, dismissed: Set<string>): ChatMessage {
  const calls = message.toolCalls;
  if (!dismissed.size || !calls || calls.length === 0) return message;
  const hidden = new Set(
    calls.filter((c) => dismissed.has(c.id) && isCancelledDelegation(c)).map((c) => c.id),
  );
  if (!hidden.size) return message;
  return {
    ...message,
    toolCalls: calls.filter((c) => !hidden.has(c.id) && !(c.parentId != null && hidden.has(c.parentId))),
  };
}
