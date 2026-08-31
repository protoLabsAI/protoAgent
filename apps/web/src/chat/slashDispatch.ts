// Dispatch a CLIENT slash command (ADR 0061) from OUTSIDE the chat composer (#3283).
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
// WHAT `null` MEANS — not "the operator navigated away". The built-in chat surface is
// mounted for the app's LIFETIME and `active` only toggles visibility (ChatSlot, #613), so
// in a normal console window a slot stays registered while the operator is on Settings or
// a plugin rail. That is deliberate: it is what lets ⌘K dispatch a chat command from any
// surface. `slashDispatchTarget()` returns null in the two cases where there is genuinely
// nothing to dispatch INTO:
//   1. the frameless desktop LAUNCHER window (ADR 0057) — a separate webview that boots
//      straight into the palette and mounts no ChatSurface at all. Its nav commands already
//      forward a serializable NavIntent to the main window; a client slash command cannot
//      cross that boundary, because the whole point of the seam is the live React closure.
//   2. a fork surface (`registerSurface({id:"chat"})`) or a plugin iframe (`slot:"chat"`)
//      holding the chat slot — ChatSlot resolves those BEFORE the built-in surface, so the
//      built-in one never renders and nothing registers here.
//
// SESSION SEMANTICS — the reason this seam exposes more than a run() function. With
// `sessionId: null` there is a mounted slot but no thread, and dispatching from outside is
// never useful, whatever the command returns:
//   • 13 of the 16 core commands (`/clear /export /publish /btw /trajectory /prompt /perf
//     /compact /effort /model /incognito /help /bypass`) `return false`. IN THE COMPOSER
//     false means "fall through": the token stays in the draft and goes to the server as
//     ordinary text, which is a visible outcome. From OUTSIDE there is no draft to fall
//     through to, so the same false is a silent no-op.
//   • `/goal` and `/watch` return TRUE and answer through `ctx.noteToThread` — which is
//     itself a no-op without a session (ChatSurface's `noteToThread` bails on `!session`).
//     So true is NOT proof that anything was shown; do not treat it as the safe subset.
//   • only `/new` (open a tab) does something real with no session.
// The rule for a caller is therefore the simple one, not a 13-command allowlist: with
// `sessionId: null`, create or focus a session first (or offer nothing but `/new`) rather
// than dispatching into the dark. `coreSlashCommands.test.ts` pins that inventory so this
// note can't quietly go stale.

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
 *  slot, or null when NO slot is registered — see the "what null means" note above. Read
 *  this before offering a command, and read it AT THAT MOMENT: the projection is a fresh
 *  object every call and the registration churns every render, so it is a point-in-time
 *  answer, never a React dependency or a `useSyncExternalStore` snapshot. */
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
 *  operator) rather than assume the command ran. True is weaker than it looks with no
 *  session — see the session-semantics note above. */
export function runSlashFromOutside(raw: string): boolean {
  const dispatcher = target;
  if (!dispatcher) return false;
  const command = raw.trim().replace(/^\/+/, "").trim();
  if (!command) return false;
  return dispatcher.run(command);
}
