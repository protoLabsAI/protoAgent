// Dispatch a CLIENT slash command (ADR 0061) from OUTSIDE the chat composer.
//
// `runClientSlash` lives inside `ChatSessionSlot` and closes over per-slot React state —
// the draft setter, the composer-form opener, the fetched server command list, the
// developer-flag predicate, `noteToThread`. None of that is reachable from another
// surface, and a caller must NEVER synthesize a stand-in SlashContext: a no-op
// `noteToThread` would silently swallow the output of every command that answers with a
// system note (/help, /perf, /trajectory, /prompt, /watch…) — the command would look like
// it ran and print nothing.
//
// So the VISIBLE slot publishes its own dispatcher here, the same imperative-seam shape
// `escapeStop.ts` uses for the Escape keybinding: module-level, last-write-wins, guarded
// unregister. Only the visible slot registers, so a dispatch always targets the session
// the operator is looking at.
//
// SESSION SEMANTICS — the reason this seam exposes more than a run() function. 13 of the
// 16 core client commands (`/clear /export /publish /btw /trajectory /prompt /perf
// /compact /effort /model /incognito /help /bypass`) `return false` when
// `ctx.sessionId` is null. IN THE COMPOSER false means "fall through": the token stays in
// the draft and goes to the server as ordinary text, which is a visible outcome. From
// OUTSIDE there is no draft to fall through to, so the same false is a silent no-op — the
// operator picks a row and nothing happens. `slashDispatchTarget()` therefore reports both
// facts a caller needs BEFORE it offers a command: whether any slot is mounted at all
// (null ⇒ the chat surface isn't up) and whether that slot has a session (`sessionId:
// null` ⇒ the session-scoped commands will decline). A caller is expected to disable, or
// open a session first, rather than dispatch into the dark.

/** The visible chat slot's dispatcher. Registered per render (see ChatSurface), so treat
 *  the object identity as per-render — only the guarded unregister compares it. */
export type SlashDispatchTarget = {
  /** Run one client command. `raw` is the command WITHOUT its leading slash, e.g.
   *  `"effort high"` — the same shape `runClientSlash` takes. Returns true if a registered
   *  command claimed the token (and is flag-on, and accepted the current session), false if
   *  it fell through. */
  run: (raw: string) => boolean;
  /** The slot's chat session id, or null when it has none. */
  sessionId: string | null;
};

let target: SlashDispatchTarget | null = null;

/** Publish the visible slot's client-slash dispatcher. Last write wins (a newly visible
 *  slot simply replaces the outgoing one); the returned unregister only clears its OWN
 *  target, so cleanup ordering across slot re-renders can't drop a fresh registration. */
export function registerSlashDispatcher(dispatcher: SlashDispatchTarget): () => void {
  target = dispatcher;
  return () => {
    if (target === dispatcher) target = null;
  };
}

/** Where a slash command dispatched right now would land: `{ sessionId }` for the visible
 *  slot, or null when NO slot is registered (chat surface not mounted). Read this before
 *  offering a session-scoped command — see the session-semantics note above. */
export function slashDispatchTarget(): { sessionId: string | null } | null {
  return target ? { sessionId: target.sessionId } : null;
}

/** Run a client slash command from outside the composer. Accepts the token with or without
 *  its leading slash (`"/help"` and `"help"` both work), so a caller holding a display
 *  token doesn't accidentally look up a command named "/help" and get a false that reads
 *  as "no chat surface".
 *
 *  Returns false when nothing handled it — no dispatcher registered, an unknown or
 *  flag-off token, or a command that declined (typically: no session). False is a real
 *  signal: the caller should fall back (open a session, keep the palette open, tell the
 *  operator) rather than assume the command ran. */
export function runSlashFromOutside(raw: string): boolean {
  const dispatcher = target;
  if (!dispatcher) return false;
  const command = raw.trim().replace(/^\/+/, "").trim();
  if (!command) return false;
  return dispatcher.run(command);
}
