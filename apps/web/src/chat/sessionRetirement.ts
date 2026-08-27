import { api } from "../lib/api";
import { chatStore, type SessionStatus } from "./chat-store";

type RetirementDeps = {
  retireRemote: (sessionId: string, harvest: boolean) => Promise<unknown>;
  deleteLocal: (sessionId: string) => void;
};

const defaults: RetirementDeps = {
  retireRemote: (sessionId, harvest) => api.deleteChatSession(sessionId, harvest),
  deleteLocal: (sessionId) => chatStore.deleteSession(sessionId),
};

/** Server retirement is the commit point for a UI delete. Keeping the local
 * handle until it succeeds makes a transport/database failure visible and
 * retryable instead of hiding a chat that the next recovery pass can restore. */
export async function retireChatSession(
  sessionId: string,
  harvest: boolean,
  deps: RetirementDeps = defaults,
): Promise<void> {
  await deps.retireRemote(sessionId, harvest);
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
