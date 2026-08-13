// The publish gesture (#2179 P2, #2683) — confirms a thread for the hosted viewer and
// posts a status note into the thread, same shape as exportChat.ts's export note. The
// PREVIEW half (what PublishDialog shows before this runs) is a separate, read-only
// fetch (api.fetchPublishPreview) — this module only covers the action taken after the
// operator has reviewed and confirmed.
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import type { ChatMessage } from "../lib/types";
import { chatStore } from "./chat-store";

function note(content: string, tone: ChatMessage["noteTone"]): ChatMessage {
  return {
    id: `sys-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role: "system",
    content,
    noteTone: tone,
    createdAt: Date.now(),
    status: "done",
  };
}

function append(sessionId: string, m: ChatMessage) {
  chatStore.updateMessages(sessionId, [
    ...(chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.messages ?? []),
    m,
  ]);
}

/**
 * Publish a chat session to the hosted viewer and post a status note into the thread.
 * Never rejects — failures (including "not configured yet", the expected state until
 * #2685's hosted service exists) surface as a note, same as exportChatToFile.
 */
export async function confirmPublish(sessionId: string, title?: string): Promise<void> {
  try {
    const res = await api.publishChatSession(sessionId, title);
    if (!res.published) {
      const tone = res.reason === "not_configured" ? "warning" : "danger";
      append(sessionId, note(res.message, tone));
      return;
    }
    const redacted = res.redactions?.length
      ? ` **${res.redactions.length} secret pattern(s) were redacted.**`
      : "";
    const missing = res.artifact_notes?.length
      ? ` ${res.artifact_notes.length} artifact(s) noted but not fully included — see the link for details.`
      : "";
    // No revocation UI exists yet (#2684, not built) — surface the raw token so the
    // operator at least has it recorded, rather than implying a management surface
    // that isn't there.
    const revoke = res.revoke_token ? `\n\nRevoke token: \`${res.revoke_token}\` (save this — no UI to look it up yet).` : "";
    append(
      sessionId,
      note(`**Published.** [${res.public_url}](${res.public_url})${redacted}${missing}${revoke}`, "success"),
    );
  } catch (e) {
    append(sessionId, note(`Publish failed — ${errMsg(e)}`, "danger"));
  }
}
