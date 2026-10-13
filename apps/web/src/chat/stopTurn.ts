import type { ChatMessage } from "../lib/types";

/** Which A2A task a Stop press should cancel (#1617).
 *
 *  The composer's Stop historically used only the slot's live `taskId` state —
 *  set when THIS ChatSurface instance started the turn. But a slot can render a
 *  turn it did not start: after a reload, a navigation remount, or on desktop
 *  where the Tauri relay pumps frames into the shared chat store regardless of
 *  which instance is mounted. In every one of those states the live taskId is
 *  empty and Stop was a silent no-op — no CancelTask on the wire, nothing to
 *  abort locally. The durable `taskId` persisted on the streaming message (the
 *  same one the self-heal reconciler uses) is the fallback that makes Stop work
 *  from any slot that can see the turn. */
export function resolveStopTarget(messages: ChatMessage[], liveTaskId: string): string {
  if (liveTaskId) return liveTaskId;
  const streaming = [...messages]
    .reverse()
    .find((m) => m.role === "assistant" && m.status === "streaming");
  return streaming?.taskId || "";
}

/** Settle every bubble a stop leaves behind: no message may stay `streaming`
 *  after the user pressed Stop. The send-loop only finalizes turns it owns —
 *  a re-attached turn has no owner in this slot, so its bubble (and any
 *  `running` tool cards) would spin forever. Partial content is kept. */
export function finalizeStoppedMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((m) => {
    if (m.role !== "assistant" || m.status !== "streaming") return m;
    const toolCalls = m.toolCalls?.map((c) =>
      c.status === "running" ? { ...c, status: "done" as const } : c,
    );
    return { ...m, status: "done" as const, toolCalls };
  });
}
