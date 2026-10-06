import { api } from "../lib/api";
import { chatStore, type SessionStatus } from "./chat-store";

/** What deleting or clearing a chat does to memory (#3493): `harvest` adds a searchable
 * summary first; `forget` removes what the chat already wrote (its compaction archives and
 * harvested summaries/facts). Harvest is ON by default for ordinary chats (#4053) — the
 * operator's "keep this out of memory" switch is incognito, not a per-delete opt-in. The two
 * are mutually exclusive in the dialogs ("just make it gone" is one forget click). */
export type ChatMemoryChoice = { harvest: boolean; forget: boolean };

/** The default for every delete without a dialog that still auto-harvests (#4053):
 * harvest unless the chat is incognito; never forget. Incognito chats leave no memory
 * (ADR 0069 D3b), so they are never harvested — the server enforces this too, as a backstop.
 * Every delete path (dialog, clear, bulk close, quick-delete) derives its default here. */
export function defaultMemoryChoice(incognito?: boolean): ChatMemoryChoice {
  return { harvest: !incognito, forget: false };
}

/** The no-op memory choice for closes that must not touch memory at all — goal-tab
 * detaches/stops keep this (their retirement is a local tab dismissal, not a harvest point). */
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
