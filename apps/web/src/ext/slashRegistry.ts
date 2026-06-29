// Build-time fork seam for CLIENT-SIDE slash commands (ADR 0061, extends ADR 0038 D3).
//
// A fork (or a core module) drops a `src/ext/<name>.tsx` — or any module imported at
// startup — that calls `registerSlashCommand()` to own a `/<name>` chat command that runs
// IN THE BROWSER, WITHOUT editing `ChatSurface.tsx`. So a `git pull upstream` stays
// conflict-free. This is the frontend twin of the backend's `register_chat_command`
// (graph/plugins/registry.py): registering a token CLAIMS it — typing or picking `/<name>`
// invokes the handler and short-circuits the send (the chat input never goes to the agent).
//
// Distinct from SERVER slash commands (`/api/chat/commands`, e.g. `/goal`, plugin `/issue`),
// which fill the draft for the user to send. Client commands act locally on pick/submit.
// Core itself registers `/new`, `/clear`, `/effort` through this seam (see
// `chat/coreSlashCommands.ts`) — the seam is the only path, not a special case.

import type { SystemNoteTone } from "../lib/types";

/** What a client slash command's handler receives. The host (ChatSurface) builds this
 *  from its local state + the chat store when the command fires. */
export type SlashContext = {
  /** Everything after the token, trimmed (e.g. `/effort high` → `"high"`). */
  rest: string;
  /** The active chat session id, or null if none. */
  sessionId: string | null;
  /** Drop a LOCAL system note into the thread (shown to the operator, never sent). `tone`
   *  colours it (info/warning/danger/success); omit for a neutral note. */
  noteToThread: (markdown: string, opts?: { tone?: SystemNoteTone }) => void;
  /** Replace the composer draft text. */
  setDraft: (text: string) => void;
  /** Return focus to the composer textarea. */
  focusComposer: () => void;
};

export type ClientSlashCommand = {
  /** The `/<name>` token (no leading slash), matched case-insensitively. */
  name: string;
  /** Shown in the slash-command menu. */
  description: string;
  /** Optional usage hint. */
  usage?: string;
  /** Run when the user picks or submits `/<name>`. Return `true` if handled (the send is
   *  short-circuited + the draft cleared); return `false` to fall through to the default
   *  (insert `/<name> ` into the draft to edit + send). Fire async work and return `true`
   *  to intercept synchronously. */
  run: (ctx: SlashContext) => boolean;
};

const _commands: ClientSlashCommand[] = [];

/** Register a client-side slash command. First registration of a token wins (HMR-safe),
 *  mirroring `registerSurface`. */
export function registerSlashCommand(cmd: ClientSlashCommand): void {
  const name = (cmd?.name || "").trim().toLowerCase();
  if (!name || typeof cmd.run !== "function") return;
  if (_commands.some((c) => c.name === name)) return; // first wins (HMR-safe)
  _commands.push({ ...cmd, name });
}

export function registeredSlashCommands(): ClientSlashCommand[] {
  return _commands;
}

/** The command owning `/<token>`, or undefined. Token matched case-insensitively. */
export function findSlashCommand(token: string): ClientSlashCommand | undefined {
  const t = (token || "").trim().toLowerCase();
  return _commands.find((c) => c.name === t);
}
