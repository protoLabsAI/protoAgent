// Core CLIENT-SIDE slash commands (ADR 0061) — registered through the SAME seam a fork
// uses (`registerSlashCommand`), so the registry is the only path, never a special case
// (the way the backend's `register_chat_command` has no core bypass). Imported for its
// side effects by ChatSurface. `/new` opens a tab, `/clear` wipes this tab's history,
// `/effort` sets this tab's reasoning effort, `/help` prints the command/shortcut
// reference. Behaviour ported verbatim from the old hardcoded `runClientSlash` switch.

import { registeredSlashCommands, registerSlashCommand } from "../ext/slashRegistry";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryClient } from "../lib/queryClient";
import { queryKeys, settingsSchemaQuery } from "../lib/queries";
import type { ChatMessage, HitlPayload } from "../lib/types";
import { chatStore, DEFAULT_REASONING_EFFORT, REASONING_EFFORTS } from "./chat-store";
import { exportChatToFile } from "./exportChat";
import { buildGoalSetBody, goalFormPayload } from "./goalForm";
import { modelChoices, modelFormPayload, modelPickerData, resolveModelArg, type ModelPickerData } from "./modelForm";

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
  name: "export",
  description: "Download this chat as Markdown (secrets redacted)",
  run: (ctx) => {
    if (!ctx.sessionId) return false; // no session → fall through
    void exportChatToFile(ctx.sessionId);
    ctx.focusComposer();
    return true;
  },
});

registerSlashCommand({
  name: "compact",
  description: "Summarize & archive older history, keeping recent context",
  flag: "chat.compact", // pre-release (ADR 0068) — hidden + inert while the flag is off
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

// One-line hint per level, shown on the picker cards (#1701).
const EFFORT_HINTS: Record<string, string> = {
  low: "Fastest — least deliberation",
  medium: "Balanced",
  high: "More deliberate reasoning",
  max: "Maximum reasoning budget",
  off: "Disable reasoning for this tab",
};

// A one-step HITL form whose single `effort` field renders as option cards (the `oneOf`
// + descriptions turn it into cards — see hitl-form.isCardChoice). `current` preselects
// the tab's active level.
function effortFormPayload(current: string): HitlPayload {
  return {
    kind: "form",
    title: "Reasoning effort",
    description: "Applies to this tab's next message.",
    steps: [
      {
        schema: {
          type: "object",
          required: ["effort"],
          properties: {
            effort: {
              type: "string",
              title: "Effort",
              default: current,
              oneOf: REASONING_EFFORTS.map((e) => ({ const: e, title: e, description: EFFORT_HINTS[e] })),
            },
          },
        },
      },
    ],
  };
}

function applyEffort(ctx: { sessionId: string; noteToThread: (m: string) => void; focusComposer: () => void }, level: string) {
  chatStore.setSessionReasoningEffort(ctx.sessionId, level);
  const off = level === "off" ? " — reasoning disabled for this tab" : "";
  ctx.noteToThread(`Reasoning effort set to **${level}**${off}. Applies to the next message.`);
  ctx.focusComposer();
}

registerSlashCommand({
  name: "effort",
  description: "Reasoning effort: low | medium | high | max | off",
  usage: "/effort low|medium|high|max|off",
  run: (ctx) => {
    const sid = ctx.sessionId;
    if (!sid) return false;
    const arg = ctx.rest.trim().toLowerCase();
    const opts = REASONING_EFFORTS.join(" · ");
    if (!arg) {
      // Bare `/effort` opens a picker form in the composer (#1701) — submit sets the tab's
      // effort locally, no agent round-trip. Falls back to a note where the host hasn't
      // wired the composer-form panel (openForm is optional on the seam).
      const session = chatStore.getSnapshot().sessions.find((s) => s.id === sid);
      const cur = session?.reasoningEffort ?? DEFAULT_REASONING_EFFORT;
      if (ctx.openForm) {
        ctx.openForm({
          payload: effortFormPayload(cur),
          onSubmit: (answers) => {
            const level = typeof answers === "object" && answers ? String((answers as Record<string, unknown>).effort ?? "") : "";
            if ((REASONING_EFFORTS as readonly string[]).includes(level)) {
              applyEffort({ sessionId: sid, noteToThread: ctx.noteToThread, focusComposer: ctx.focusComposer }, level);
            }
          },
          onCancel: ctx.focusComposer,
        });
      } else {
        ctx.noteToThread(`Reasoning effort: **${cur}**. Set it with \`/effort ${REASONING_EFFORTS.join("|")}\`.`);
        ctx.focusComposer();
      }
    } else if ((REASONING_EFFORTS as readonly string[]).includes(arg)) {
      applyEffort({ sessionId: sid, noteToThread: ctx.noteToThread, focusComposer: ctx.focusComposer }, arg);
    } else {
      ctx.noteToThread(`Unknown effort \`${arg}\`. Options: ${opts}.`);
      ctx.focusComposer();
    }
    return true;
  },
});

// --- /model — quick-switch this tab's model from the pinned favorites (#1957) ---------

/** The tab's effective model: its override, else the configured default. */
function currentModelOf(sessionId: string, data: ModelPickerData): string {
  const session = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  return session?.model || data.globalModel;
}

/** Apply a pick to the tab. Choosing the configured default CLEARS the per-tab override
 *  (mirrors ComposerModelSelect), so the tab tracks future default changes again. */
function applyModel(
  ctx: { sessionId: string; noteToThread: (m: string) => void; focusComposer: () => void },
  alias: string,
  globalModel: string,
) {
  const isDefault = !alias || alias === globalModel;
  chatStore.setSessionModel(ctx.sessionId, isDefault ? "" : alias);
  ctx.noteToThread(
    isDefault
      ? `Model reset to the configured default${globalModel ? ` (**${globalModel}**)` : ""} for this tab. Applies to the next message.`
      : `Model set to **${alias}** for this tab. Applies to the next message.`,
  );
  ctx.focusComposer();
}

registerSlashCommand({
  name: "model",
  description: "Switch this tab's model — bare /model picks from your favorites",
  usage: "/model [alias|default]",
  run: (ctx) => {
    const sid = ctx.sessionId;
    if (!sid) return false;
    // Under an ACP runtime the turn is driven by an external coding agent, not a gateway
    // model — a pick would be inert (mirrors ComposerModelSelect, which hides its menu).
    // Cache-only read: the app shell fetches runtime status at boot, so this is warm;
    // when genuinely unknown we proceed rather than block the command.
    const runtime = queryClient.getQueryData<{ agent_runtime?: string }>(queryKeys.runtime);
    const agentRuntime = runtime?.agent_runtime ?? "";
    if (agentRuntime.startsWith("acp:")) {
      ctx.noteToThread(
        `This chat runs on the **${agentRuntime.slice(4)}** coding agent (\`agent_runtime: ${agentRuntime}\`) — gateway model switching doesn't apply.`,
        { tone: "info" },
      );
      ctx.focusComposer();
      return true;
    }
    const arg = ctx.rest.trim();
    // The favorites + model list live in the settings schema — the SAME source the
    // composer's model menu reads. ensureQueryData serves the warm cache (5-min
    // staleTime) and only fetches on a cold start, so the picker opens instantly.
    void queryClient
      .ensureQueryData(settingsSchemaQuery())
      .then((schema) => {
        const data = modelPickerData(schema.groups);
        const applyCtx = { sessionId: sid, noteToThread: ctx.noteToThread, focusComposer: ctx.focusComposer };
        if (arg) {
          // Typed form: `/model <alias>` applies directly (like `/effort high`);
          // `/model default` clears the override without needing the default favorited.
          if (/^(default|reset)$/i.test(arg)) {
            applyModel(applyCtx, "", data.globalModel);
            return;
          }
          const alias = resolveModelArg(data, arg);
          if (alias) {
            applyModel(applyCtx, alias, data.globalModel);
          } else {
            const known = data.favorites.length ? data.favorites : data.models.slice(0, 8);
            ctx.noteToThread(
              `Unknown model \`${arg}\`.${known.length ? ` Known: ${known.join(" · ")}.` : ""} Bare \`/model\` opens the picker.`,
              { tone: "warning" },
            );
            ctx.focusComposer();
          }
          return;
        }
        // Bare /model → the card picker: favorites when pinned, else the full gateway
        // list with a hint to pin favorites (graceful no-favorites fallback, #1957).
        const { choices } = modelChoices(data);
        if (!choices.length) {
          ctx.noteToThread("No models available from the gateway — configure one in Settings ▸ Model.", { tone: "warning" });
          ctx.focusComposer();
          return;
        }
        if (!ctx.openForm) {
          // Host without the composer-form panel (optional seam) — degrade to a note.
          const cur = currentModelOf(sid, data);
          ctx.noteToThread(
            `Model for this tab: **${cur || "(gateway default)"}**. Switch with \`/model <alias>\`; pin favorites in Settings ▸ Model.`,
          );
          ctx.focusComposer();
          return;
        }
        ctx.openForm({
          payload: modelFormPayload(data, currentModelOf(sid, data)),
          onSubmit: (answers) => {
            const alias =
              typeof answers === "object" && answers ? String((answers as Record<string, unknown>).model ?? "") : "";
            if (alias && choices.includes(alias)) applyModel(applyCtx, alias, data.globalModel);
          },
          onCancel: ctx.focusComposer,
        });
      })
      .catch(() => {
        ctx.noteToThread("Couldn't load the model list — try again, or check Settings ▸ Model.", { tone: "danger" });
        ctx.focusComposer();
      });
    return true;
  },
});

registerSlashCommand({
  name: "incognito",
  description: "Incognito for this tab: turns leave no memory and inject none",
  usage: "/incognito on|off",
  run: (ctx) => {
    if (!ctx.sessionId) return false;
    const arg = ctx.rest.trim().toLowerCase();
    const session = chatStore.getSnapshot().sessions.find((s) => s.id === ctx.sessionId);
    const cur = !!session?.incognito;
    const next = arg === "on" ? true : arg === "off" ? false : !cur; // bare /incognito toggles
    chatStore.setSessionIncognito(ctx.sessionId, next);
    ctx.noteToThread(
      next
        ? "**Incognito ON** for this tab — every message now carries `incognito`: no session summary, no memory harvest, no memory injection, until you turn it off with `/incognito off`. Messages already sent before this were NOT incognito."
        : "Incognito **off** — turns persist to memory and receive memory again.",
      { tone: "info" },
    );
    ctx.focusComposer();
    return true;
  },
});

registerSlashCommand({
  name: "help",
  description: "Show available commands & shortcuts",
  run: (ctx) => {
    if (!ctx.sessionId) return false; // no thread to print into → fall through
    // Enumerate the LIVE registry with the host's own visibility rules (ADR 0068):
    // flag-tagged commands appear only while their flag is on — fail-closed, exactly
    // like the composer's slash menu — so the card never advertises a dead command.
    const flagOn = ctx.flagOn ?? (() => false);
    const client = registeredSlashCommands().filter((c) => !c.flag || flagOn(c.flag));
    const seen = new Set(client.map((c) => c.name));
    // Server commands (/goal, plugin commands…) reflect what's actually installed. A
    // client command CLAIMS its token, so drop server duplicates (menu order: client first).
    const server = (ctx.serverCommands ?? []).filter((c) => !seen.has(c.name.toLowerCase()));
    const row = (name: string, description: string) => `- \`/${name}\` — ${description}`;
    ctx.noteToThread(
      [
        "**Commands**",
        ...client.map((c) => row(c.name, c.description)),
        ...server.map((c) => row(c.name, c.description)),
        "",
        "**Shortcuts**",
        "- `Enter` send · `⌘/Ctrl+Enter` newline · `↑` recall input history · `/` command menu",
        "- While the agent is working, `Enter` queues a steer into the running turn",
        "- `Shift+click` (or focus the `+` and `Shift+Enter`/`Shift+Space`) → new incognito chat (also on a tab's right-click menu)",
        "- `Shift+click` a tab's ✕ → delete the chat without the confirm dialog",
        "",
        "**Capabilities** — watches, schedules, tasks, and goals all run from chat; manage them from the rail surfaces.",
      ].join("\n"),
    );
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

// `/goal new` opens a guided goal-creation form (ADR 0073 completion contracts, Part 2) in
// the composer — the SAME `HitlForm` + `openForm` seam as `/effort`'s picker, resolved
// locally (no agent round-trip): on submit it POSTs the operator goal-set. ONLY the `new`
// subcommand is claimed client-side; bare `/goal` (status), `/goal <text>` (set), and
// `/goal clear` all fall through (return false) to the SERVER `/goal` control command
// (`graph/slash_commands.py`), so none of the existing behavior regresses. A client command
// registered here CLAIMS the `/goal` token in the menu (like `/clear` already does) — its
// row surfaces the `/goal new` affordance; picking it inserts `/goal ` to edit as before.
registerSlashCommand({
  name: "goal",
  // Prefix kept as the server /goal's "Set or check goals" so the /help card is unchanged
  // (this client row shadows the server one in the deduped list) — plus the new affordance.
  description: "Set or check goals — /goal new opens a guided form",
  usage: "/goal new · /goal <text> · /goal clear",
  run: (ctx) => {
    const arg = ctx.rest.trim().toLowerCase();
    if (arg !== "new") return false; // bare/set/clear → server `/goal` unchanged
    const sid = ctx.sessionId;
    if (!sid || !ctx.openForm) {
      // No tab to own the goal, or a host that never wired the composer-form panel: don't
      // send "/goal new" on to the server (it would set a goal literally named "new").
      ctx.noteToThread(
        "Open a chat tab to set a goal here, or use `/goal <text>` to set one inline.",
        { tone: "info" },
      );
      ctx.focusComposer();
      return true;
    }
    ctx.openForm({
      payload: goalFormPayload(),
      onSubmit: (answers) => {
        const body = buildGoalSetBody(
          sid,
          typeof answers === "object" && answers ? (answers as Record<string, unknown>) : {},
        );
        if (!body) {
          ctx.noteToThread("A goal needs a condition — nothing was set.", { tone: "warning" });
          ctx.focusComposer();
          return;
        }
        void api
          .setGoal(body)
          .then((res) =>
            ctx.noteToThread(`**Goal set.** ${res.message ?? ""}`.trim(), { tone: "success" }),
          )
          // A rejected verifier / disabled goal mode comes back as HTTP 400 → request() throws.
          .catch((e) => ctx.noteToThread(`Couldn't set goal: ${errMsg(e)}`, { tone: "danger" }));
        ctx.focusComposer();
      },
      onCancel: ctx.focusComposer,
    });
    return true;
  },
});
