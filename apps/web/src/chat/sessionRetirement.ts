import { api } from "../lib/api";
import { chatStore, type SessionStatus } from "./chat-store";

/** What deleting or clearing a chat does to memory. Both halves are opt-in (#3493):
 * `harvest` adds a searchable summary first; `forget` removes what the chat already
 * wrote (its compaction archives and harvested summaries/facts). */
export type ChatMemoryChoice = { harvest: boolean; forget: boolean };

/** The default for every delete without a dialog (quick-delete, bulk close). */
export const NO_MEMORY_CHANGE: ChatMemoryChoice = { harvest: false, forget: false };

type RetirementDeps = {
  retireRemote: (sessionId: string, memory: ChatMemoryChoice) => Promise<unknown>;
  deleteLocal: (sessionId: string) => void;
};

const defaults: RetirementDeps = {
  retireRemote: (sessionId, memory) => api.deleteChatSession(sessionId, memory.harvest, memory.forget),
  deleteLocal: (sessionId) => chatStore.deleteSession(sessionId),
};

/** Server retirement is the commit point for a UI delete. Keeping the local
 * handle until it succeeds makes a transport/database failure visible and
 * retryable instead of hiding a chat that the next recovery pass can restore. */
export async function retireChatSession(
  sessionId: string,
  memory: ChatMemoryChoice,
  deps: RetirementDeps = defaults,
): Promise<void> {
  await deps.retireRemote(sessionId, memory);
  deps.deleteLocal(sessionId);
}

/** Clear keeps the session id reusable, so it has no tombstone protection
 * against a producer saving the old turn after the wipe. The console therefore
 * permits clear only once that producer is no longer active. */
export function canClearSession(
  status: SessionStatus | undefined,
  hasServerTurn = false,
): boolean {
  return status !== "streaming" && !hasServerTurn;
}
