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
// the operator would be looking at — whether the chat SURFACE holding it is itself on
// screen is a separate question, and a separate field (see "visibility semantics").
//
// WHAT `null` MEANS — not "the operator navigated away". The built-in chat surface is
// mounted for the app's LIFETIME and `active` only toggles visibility (ChatSlot, #613), so
// in a normal console window a slot stays registered while the operator is on Settings or
// a plugin rail. That is deliberate: it is what lets ⌘K dispatch a chat command from any
// surface — but it is ALSO why the target reports `surfaceActive` (see below), because a
// registered slot is not the same as a slot the operator can see.
// `slashDispatchTarget()` returns null in the two cases where there is genuinely
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
//
// VISIBILITY SEMANTICS — the same hazard on the other axis, and the reason `surfaceActive`
// rides along too. The whole chat surface renders under `display: none` when it is not the
// active rail surface (ChatSurface's <section>), so with the operator on Settings the slot
// is registered, has a session, and `run` returns true — while every command that answers
// through `ctx.noteToThread` (`/help`, `/perf`, `/trajectory`, `/prompt`, `/watch`, `/goal`)
// writes its note into a subtree nobody is looking at, and `/effort` + `/model` open their
// picker in a hidden composer panel. That is the same silent success this seam exists to
// prevent, just triggered by visibility instead of a missing session, and a caller cannot
// infer it from `sessionId`.
// So: with `surfaceActive: false`, RAISE THE CHAT SURFACE FIRST — `openView("chat")` in
// app/usePaletteRegistry, which is what every other console navigation funnels through —
// and dispatch after. (The seam does not raise it itself on purpose: `chat/` deliberately
// imports nothing from `app/`, and a command like `/clear` or `/bypass` is a legitimate
// thing to fire without yanking the operator off the surface they are on. The caller knows
// which of the two it is doing; the seam does not.) Dispatching immediately after the
// raise is fine — the note lands in the session's message list, which renders when the
// surface does; only the pre-raise render still reports `surfaceActive: false`.

// WHY THE SEAM ALSO CARRIES A DRAFT SETTER — the user-facing SKILL case (#3285). A server
// `user_facing` skill (`/api/chat/commands`, kind "skill") is NOT a thing an outside
// surface can run: it is a message REWRITE the server applies on the NEXT SEND
// (server/chat_commands.py `_skill_directive` injects the procedure and falls through to a
// normal lead-agent turn). There is no "run this skill" call anywhere, and inventing one
// from the palette would mean sending a message on the operator's behalf.
// So the honest action for a skill is the composer's own: put `/<skill> ` in the draft and
// hand the operator the caret. That needs the SAME live React closure `run` needs — the
// draft is `useState` inside ChatSessionSlot (seeded from sessionStorage on mount), so
// writing sessionStorage from outside would be swallowed by the mounted slot and a store
// write has nowhere to go. Hence `prefillDraft` rides the same registration rather than a
// second parallel seam.

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
  /** Whether the chat SURFACE is the active rail surface — i.e. whether anything this
   *  command draws (a system note, the /effort picker) would actually be on screen. False
   *  while the operator is on another rail; the slot stays registered anyway (#613). */
  surfaceActive: boolean;
  /** Replace this slot's composer draft with `text` and focus the textarea — the "hand the
   *  operator a half-written message" verb, for a token that CANNOT be run from outside
   *  (a user-facing skill) and for anything else that wants the send left to the operator.
   *  Required, not optional: a host that owns the chat slot owns a composer by definition,
   *  and an optional setter would invite a caller to skip the check and silently drop the
   *  prefill — exactly the failure mode this seam exists to prevent. */
  prefillDraft: (text: string) => void;
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

/** Where a slash command dispatched right now would land: `{ sessionId, surfaceActive }`
 *  for the visible slot, or null when NO slot is registered — see the "what null means"
 *  note above. Both fields are conditions a caller has to check, not decoration: with
 *  `sessionId: null` a command has no thread to act on, and with `surfaceActive: false` its
 *  output would be drawn in a hidden surface. Read this before offering a command, and read
 *  it AT THAT MOMENT: the projection is a fresh object every call and the registration
 *  churns every render, so it is a point-in-time answer, never a React dependency or a
 *  `useSyncExternalStore` snapshot. */
export function slashDispatchTarget(): { sessionId: string | null; surfaceActive: boolean } | null {
  return target ? { sessionId: target.sessionId, surfaceActive: target.surfaceActive } : null;
}

/** Run a client slash command from outside the composer. Accepts the token with or without
 *  its leading slash (`"/help"` and `"help"` both work), so a caller holding a display
 *  token doesn't accidentally look up a command named "/help" and get a false that reads
 *  as "no chat surface".
 *
 *  Returns false when nothing handled it — no dispatcher registered, an unknown or
 *  flag-off token, or a command that declined (typically: no session). False is a real
 *  signal: the caller should fall back (open a session, keep the palette open, tell the
 *  operator) rather than assume the command ran. True is weaker than it looks: it means a
 *  command CLAIMED the token, not that the operator saw anything — check
 *  `slashDispatchTarget()` for a session and a visible surface first (see the session- and
 *  visibility-semantics notes above). */
export function runSlashFromOutside(raw: string): boolean {
  const dispatcher = target;
  if (!dispatcher) return false;
  const command = raw.trim().replace(/^\/+/, "").trim();
  if (!command) return false;
  return dispatcher.run(command);
}

/** Put `text` in the visible slot's composer draft and focus it, leaving the SEND to the
 *  operator. The action for a user-facing skill, which is a server-side message rewrite
 *  rather than anything a caller can invoke (see the note above `SlashDispatchTarget`).
 *
 *  Returns false when no slot is registered — the same real signal `runSlashFromOutside`
 *  returns, and the caller must treat it the same way (fall back, don't assume it landed).
 *  Note the two conditions on `slashDispatchTarget()` still apply and this function does
 *  NOT check them for you: with `sessionId: null` there is no slot state to write into, and
 *  with `surfaceActive: false` the operator is looking at another rail and would never see
 *  the draft appear — raise the chat surface first, exactly as for a dispatched command. */
export function prefillChatDraft(text: string): boolean {
  const dispatcher = target;
  if (!dispatcher) return false;
  dispatcher.prefillDraft(text);
  return true;
}
