/** Open a decision's source chat (switch agent tab + chat), shared by the Work view's
 *  decision detail and the chat rail's Needs-you cards (S7). A chat that isn't hydrated in
 *  the local store yet is restored from its durable turns first, so a decision raised by a
 *  background or scheduled session still lands somewhere readable. Resolves false when the
 *  source chat can't be reached — the caller owns how that is reported (WorkView toasts). */
import { api } from "../lib/api";
import { sessionFromDurableTurns, SESSION_TURN_LIMIT } from "../chat/sessionHydration";
import { openView } from "../app/palette/nav";
import { chatStore } from "../chat/chat-store";
import { useKbIntents } from "../keybindings/intents";
import { useUI } from "../state/uiStore";

export async function openSourceChat(sourceSessionId: string, reply = false): Promise<boolean> {
  try {
    if (!chatStore.getSnapshot().sessions.some((session) => session.id === sourceSessionId)) {
      const { turns, live_task_id } = await api.chatSessionTurns(sourceSessionId, SESSION_TURN_LIMIT);
      const session = sessionFromDurableTurns({ session_id: sourceSessionId,
        last_updated: turns[0]?.last_updated ?? null, turn_count: turns.length }, turns, live_task_id);
      if (!session) throw new Error("The source chat is unavailable.");
      chatStore.restoreDismissedSession(sourceSessionId);
      const token = chatStore.captureHydrationEligibility(sourceSessionId);
      if (token) chatStore.hydrateSessions([session], [token]);
    }
    if (!chatStore.getSnapshot().sessions.some((session) => session.id === sourceSessionId)) {
      throw new Error("The source chat is unavailable.");
    }
  } catch {
    return false;
  }
  chatStore.switchSession(sourceSessionId);
  useUI.getState().closeSurfaceModal();
  openView("chat");
  if (reply) useKbIntents.getState().focusComposer();
  return true;
}
