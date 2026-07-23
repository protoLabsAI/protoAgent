// The chat-export gesture (#2158 P1) — shared by the `/export` slash command AND the
// tab context menu, so both go through one path: call the server, download the Markdown,
// and post a system note summarizing the result (including what redaction removed, since
// the operator is meant to review before sharing).
import { api } from "../lib/api";
import { downloadTextFile, safeFilename } from "../lib/download";
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

function titleOf(sessionId: string): string | undefined {
  return chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.title;
}

/**
 * Export a chat session to a downloaded Markdown file and post a status note into the
 * thread. Read-only server-side; safe to run anytime. Resolves after the note is posted
 * (never rejects — failures surface as a danger note).
 */
export async function exportChatToFile(sessionId: string): Promise<void> {
  const title = titleOf(sessionId);
  const append = (m: ChatMessage) =>
    chatStore.updateMessages(sessionId, [
      ...(chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.messages ?? []),
      m,
    ]);

  try {
    const res = await api.exportChatSession(sessionId, title);
    if (!res.found) {
      // Empty thread / no checkpoint — the server explains which.
      append(note(res.message, "warning"));
      return;
    }
    const filename = `${safeFilename(title ?? "chat")}.md`;
    if (!downloadTextFile(filename, res.markdown)) {
      // Programmatic downloads blocked (sandbox / policy) — say so instead of claiming
      // success; the operator would otherwise hunt for a file that was never written.
      append(note("Export blocked on this surface — no file was written", "danger"));
      return;
    }
    const redacted = res.redactions?.length
      ? ` **${res.redactions.length} secret pattern(s) were redacted** (${res.redactions.join(", ")}) — read the file before sharing.`
      : "";
    append(
      note(
        `**Exported ${res.message_count} message(s) → \`${filename}\` (check your browser downloads).**${redacted}`,
        res.redactions?.length ? "warning" : "success",
      ),
    );
  } catch (e) {
    append(note(`Export failed — ${errMsg(e)}`, "danger"));
  }
}
