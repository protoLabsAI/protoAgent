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
  /** Optional developer-flag id (ADR 0068). When set, the host lists and dispatches the
   *  command only while the flag resolves ON (`useFlagPredicate`) — a flag-off command
   *  behaves as if it were never registered (the token falls through to the server /
   *  draft path). Registration itself is unconditional, so flipping the flag needs no
   *  re-registration. */
  flag?: string;
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

/** Parse the slash-command token the caret currently sits in — powers the MID-INPUT
 *  popover (#1530), so typing "/" opens the menu at ANY cursor position, not only when
 *  "/" is the first character. A token is a "/" at the start of the input OR immediately
 *  after whitespace, running up to the caret with no intervening whitespace. Returns the
 *  `query` (the text after the "/", up to the caret — what filters the popover), the token's
 *  `start` index, and its `end` index (the token runs to the next whitespace, which may be
 *  PAST the caret when completing mid-token — both bound a caret-anchored replace that
 *  preserves surrounding text), or null when the caret is not inside a token. */
export function slashTokenAt(
  text: string,
  caret: number,
): { query: string; start: number; end: number } | null {
  const pos = Math.max(0, Math.min(caret, text.length));
  let start = pos;
  while (start > 0 && !/\s/.test(text[start - 1])) start -= 1;
  if (text[start] !== "/") return null;
  // End of the token = next whitespace at/after the caret, so completing with the caret in
  // the MIDDLE of "/token" replaces the whole token, not just up to the caret (no tail left).
  let end = pos;
  while (end < text.length && !/\s/.test(text[end])) end += 1;
  return { query: text.slice(start + 1, pos), start, end };
}

/** The command name of a user message that IS a slash command (e.g. "/goal ship it" →
 *  "goal"), or null. Used to render an issued command as a distinct user bubble (#1529).
 *  Anchored at the start and requiring a letter-led word that ends at whitespace or EOL, so
 *  a file path like "/home/user" (a "/" followed by more path) is NOT mistaken for one. */
export function slashCommandName(content: string): string | null {
  const m = /^\/([a-z][\w-]*)(?=\s|$)/i.exec(content.trim());
  return m ? m[1] : null;
}
