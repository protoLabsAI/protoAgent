// Core CLIENT-SIDE slash commands (ADR 0061) — registered through the SAME seam a fork
// uses (`registerSlashCommand`), so the registry is the only path, never a special case
// (the way the backend's `register_chat_command` has no core bypass). Imported for its
// side effects by ChatSurface. `/new` opens a tab, `/clear` wipes this tab's history,
// `/effort` sets this tab's reasoning effort. Behaviour ported verbatim from the old
// hardcoded `runClientSlash` switch.

import { registerSlashCommand } from "../ext/slashRegistry";
import { api } from "../lib/api";
import type { ChatMessage } from "../lib/types";
import { chatStore, DEFAULT_REASONING_EFFORT, REASONING_EFFORTS } from "./chat-store";

// Local id for the system notes /compact posts (the command manages messages
// directly, like /clear, so it needs to own the ids it can later replace).
function noteId() {
  return `sys-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

registerSlashCommand({
  name: "new",
  description: "Open a new chat tab",
  run: (ctx) => {
    chatStore.createSession();
    ctx.focusComposer();
    return true;
  },
});

registerSlashCommand({
  name: "clear",
  description: "Clear this chat's history",
  run: (ctx) => {
    if (!ctx.sessionId) return false; // no session → not handled (falls through)
    void api.deleteChatSession(ctx.sessionId, false).catch(() => {});
    chatStore.updateMessages(ctx.sessionId, []);
    ctx.focusComposer();
    return true;
  },
});

registerSlashCommand({
  name: "compact",
  description: "Summarize & archive older history, keeping recent context",
  run: (ctx) => {
    if (!ctx.sessionId) return false; // no session → fall through
    const sessionId = ctx.sessionId;
    const messagesOf = () =>
      chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.messages ?? [];

    // Optimistic note (own id so we can drop it once the server responds).
    const pendingId = noteId();
    chatStore.updateMessages(sessionId, [
      ...messagesOf(),
      {
        id: pendingId,
        role: "system",
        content: "Compacting this conversation — archiving older history and summarizing…",
        noteTone: "info",
        createdAt: Date.now(),
        status: "done",
      },
    ]);
    ctx.focusComposer();

    const note = (content: string, tone: ChatMessage["noteTone"]): ChatMessage => ({
      id: noteId(),
      role: "system",
      content,
      noteTone: tone,
      createdAt: Date.now(),
      status: "done",
    });
    // Drop only the optimistic note — preserve anything that streamed in meanwhile.
    const withoutPending = () => messagesOf().filter((m) => m.id !== pendingId);

    void api
      .compactChatSession(sessionId)
      .then((res) => {
        // Never-lossy: only drop history when the server actually rewrote the
        // checkpoint (archived + removed > 0). Otherwise just surface the status.
        if (res.refused || !res.archived || res.removed <= 0) {
          chatStore.updateMessages(sessionId, [
            ...withoutPending(),
            note(res.message, res.refused ? "warning" : "info"),
          ]);
          return;
        }
        // Mirror the server: replace the view with a summary bubble + the recent
        // tail. Slice from the CURRENT messages (minus the pending note) so nothing
        // that arrived during the compaction is lost.
        const kept = res.kept > 0 ? withoutPending().slice(-res.kept) : [];
        const summary = note(
          `**Conversation compacted.** ${res.message}\n\n---\n\n${res.summary}`,
          "success",
        );
        chatStore.updateMessages(sessionId, [summary, ...kept]);
      })
      .catch(() => {
        chatStore.updateMessages(sessionId, [
          ...withoutPending(),
          note("Compaction failed — nothing was changed.", "danger"),
        ]);
      });

    return true;
  },
});

registerSlashCommand({
  name: "effort",
  description: "Reasoning effort: low | medium | high | max | off",
  usage: "/effort low|medium|high|max|off",
  run: (ctx) => {
    if (!ctx.sessionId) return false;
    const arg = ctx.rest.trim().toLowerCase();
    const opts = REASONING_EFFORTS.join(" · ");
    if (!arg) {
      const session = chatStore.getSnapshot().sessions.find((s) => s.id === ctx.sessionId);
      const cur = session?.reasoningEffort ?? `${DEFAULT_REASONING_EFFORT} (default)`;
      ctx.noteToThread(`Reasoning effort: **${cur}**. Set it with \`/effort ${REASONING_EFFORTS.join("|")}\`.`);
    } else if ((REASONING_EFFORTS as readonly string[]).includes(arg)) {
      chatStore.setSessionReasoningEffort(ctx.sessionId, arg);
      const off = arg === "off" ? " — reasoning disabled for this tab" : "";
      ctx.noteToThread(`Reasoning effort set to **${arg}**${off}. Applies to the next message.`);
    } else {
      ctx.noteToThread(`Unknown effort \`${arg}\`. Options: ${opts}.`);
    }
    ctx.focusComposer();
    return true;
  },
});

registerSlashCommand({
  name: "bypass",
  description: "DANGER: auto-approve tool permissions (run_command) for this tab",
  usage: "/bypass on|off",
  run: (ctx) => {
    if (!ctx.sessionId) return false;
    const arg = ctx.rest.trim().toLowerCase();
    const session = chatStore.getSnapshot().sessions.find((s) => s.id === ctx.sessionId);
    const cur = !!session?.bypassPermissions;
    const next = arg === "on" ? true : arg === "off" ? false : !cur; // bare /bypass toggles
    chatStore.setSessionBypassPermissions(ctx.sessionId, next);
    ctx.noteToThread(
      next
        ? "**Bypass permissions ON** for this tab — `run_command` runs **without approval** until you turn it off with `/bypass off`. (A host can forbid this entirely via `filesystem.bypass_allowed: false`.)"
        : "Bypass permissions **off** — tool approvals will prompt again.",
      { tone: next ? "warning" : "info" },
    );
    ctx.focusComposer();
    return true;
  },
});
