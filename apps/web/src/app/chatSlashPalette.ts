// The chat's own verbs in ⌘⇧K (#3292) — the client slash commands (ADR 0061) and every
// server user-facing SKILL (ADR 0052), which until now existed ONLY inside the composer's
// `/` menu. They are the console's real actions ("clear this chat", "switch model",
// "export"), and the palette — the console's one "how do I do X?" surface — did not know
// a single one of them.
//
// Two row families with DIFFERENT semantics, and getting that difference wrong is the
// whole risk in this file:
//
//   • a CLIENT slash command RUNS — dispatched through the `slashDispatch` seam (#3283)
//     into the visible chat slot's live React closure.
//   • a user-facing SKILL CANNOT BE RUN. It is a message REWRITE the server applies on the
//     NEXT SEND (server/chat_commands.py `_skill_directive` injects the procedure and falls
//     through to an ordinary lead-agent turn). There is no "run this skill" endpoint, and
//     the palette must not invent one by sending a message for the operator. So a skill row
//     PREFILLS the composer draft and stops — labelled and hinted so the row promises
//     exactly that, and never reads as "this ran".
//
// So a row either RUNS or DRAFTS, and a drafting row always says the same thing
// ("drafts in chat — you send it"), because one phrase should mean one behaviour. Every
// skill drafts; three client commands do too — `/btw` (which needs a question the palette
// has no way to ask for) and the two per-tab MODES, `/bypass` and `/incognito`.
//
// ── WHY A MODE MUST NOT BE A ONE-ENTER ROW ───────────────────────────────────────────
// `/bypass` and `/incognito` TOGGLE when dispatched bare (coreSlashCommands: `next = arg ===
// "on" ? true : arg === "off" ? false : !cur`). In the composer that is fine — the operator
// typed the whole token deliberately, at a tab whose current mode is on screen as a chip.
// From a FUZZY SEARCH it is not: the palette preselects the first match and Enter runs it,
// so "yolo" + Enter would arm `run_command` auto-approval — a trust boundary — in a
// direction the row's label never named. Nor is a directional PAIR (`/bypass on` +
// `/bypass off`) safe here: the DS matcher is a case-insensitive SUBSTRING test over the
// row's whole label/hint/keywords, and "on" is a substring of half the English in them, so
// which of the pair a query preselects is not something this file can guarantee.
//
// So a mode row DRAFTS: it raises chat, types `/bypass ` into the composer, and stops. The
// operator supplies the direction and the send, lands on the tab whose mode is changing, and
// reads the command's own system note afterwards. Nothing in the palette can arm a
// permission. The row still earns its place, because it answers the question an operator
// actually opens ⌘⇧K with — the label carries the mode's CURRENT VALUE (`… — now off`), which
// nothing else outside the chat tab tells them.
//
// ── The row's two slots ──────────────────────────────────────────────────────────────
// LABEL = `/token · what it does`, the composer `/` menu's own shape and the reason this
// reads as the same list in a second place. HINT = the row's CAVEAT (a disabled reason, the
// draft promise, "opens a chat first"), or nothing — which leaves it free for the live
// keybinding combo. The description could not live in the hint: the hint is occupied
// precisely when the operator needs the prose most (with no chat open, every client row's
// hint is a reason; a skill row's is always its draft promise), and a column of bare tokens
// — `/perf`, `/btw`, `/postmortem` — is a list you cannot shop from.
//
// ── Why the rows are STATIC, and what makes them live ────────────────────────────────
// They were a `registerPaletteSource` at first, for two good reasons — the skill list is
// live server state, and a client row's `disabled` state tracks the chat slot's session —
// and a source was still the wrong path, on three counts that have nothing to do with how
// live the data is:
//
//   • RANKING. A source's rows are ORDERED, never re-filtered or scored against the corpus
//     (palette/rootView.tsx: `orderCommands(live, …)` after `rankCommands(local, …)`), so
//     they land after every static and every surface however well they match. `/clear` under
//     the query "clear" belongs at the top, not below the whole "Go to" list — and with ~80
//     commands arriving across the sibling PRs, ranking is what keeps any of them findable.
//   • COST, in windows that get nothing for it. The root view shows "Searching…" and
//     debounces 120ms whenever ANY provider declares `getCommands` — including the frameless
//     desktop launcher, which mounts no chat and could never be served a row here. Core
//     shipping a source put that spinner in front of every keystroke everywhere.
//   • STALENESS, which is what made it urgent. A provider's results outlive the query they
//     answered: the loop only overwrites them when a read RESOLVES. Our own root view now
//     stamps and drops them (rootView.tsx), but the DS's `CommandsBody` still does not
//     (protoContent#504) — and a row that RUNS something should not depend on that being
//     fixed everywhere it might be rendered.
//
// So the rows take the synchronous, client-filtered, RANKED static path, and the host
// (`palette/registry.ts`) keeps them live by RE-REGISTERING them: it subscribes to the chat
// store through `chatPaletteSignature()` — everything a row renders from, as one string, so
// a streamed token doesn't churn the palette — and to the shared `/api/chat/commands` query
// for the skills. Same liveness, ranked with everything else, nothing paid by a window that
// cannot use it.
//
// ── SESSION SEMANTICS — decided per command, not guessed ─────────────────────────────
// Most client commands `return false` without a session, and two more answer through a
// `noteToThread` that is itself a no-op without one (slashDispatch.ts spells out the
// inventory; coreSlashCommands.test.ts pins it). In the COMPOSER false means "fall through
// to the draft" — a visible outcome. From the palette there is no draft to fall through to,
// so the same false is a row that visibly does NOTHING. Every command therefore lands in
// one of three buckets, on ONE question: does it need THIS chat, or merely A thread?
//
//   NEEDS THIS CHAT → the row is DISABLED with the reason in `hint`.
//     /clear /export /publish /btw /trajectory /prompt /perf /compact /bypass /incognito
//     The first eight read or rewrite accumulated history; auto-creating a blank tab to run
//     them would be theater (exporting an empty transcript "succeeds" and tells the operator
//     nothing). The last two are per-TAB modes: their row's whole value is naming the current
//     one, and there is no current one without a tab. Disabled-and-explaining beats hidden
//     (the row stays discoverable) and beats a dead-looking runnable row.
//
//   NEEDS ONLY A THREAD → the row CREATES OR SWITCHES to one first, then dispatches.
//     /help /effort /model /goal /watch
//     Two shapes, same requirement: a place to print (`/help`, `/watch`, `/goal`'s form) or
//     a tab to configure before you type into it (`/effort`, `/model`). A fresh tab is a
//     perfectly good answer to both — and `chatStore.createSession` hands back a pristine
//     blank instead of piling one up, so this is "focus the empty tab you already have" far
//     more often than it is "make a new one".
//
//   NEEDS NOTHING → `/new` — with one gate, because `createSession` REUSES a pristine blank
//     rather than making a second one (chat-store.ts). When that blank IS the tab you are
//     looking at, `/new` is a genuine no-op, which is why every other "new chat" affordance
//     (MobileShell, SessionSheet) disables itself on `unusedSession`. The palette row does
//     the same rather than being the one affordance that quietly does nothing.
//
// Note the gate is "is there a session", NOT "does it have messages". That is deliberate:
// the palette must not invent a stricter rule than the composer, where `/export` on an
// empty tab is allowed and simply exports an empty chat.
//
// ── The two waits ────────────────────────────────────────────────────────────────────
// Both live in `whenSlotSettles`. A raise (`navigate`) and a `createSession` are both store
// writes that only take effect on React's next commit: the slot re-registers its dispatcher
// per render, so reading the seam back synchronously would still see the OLD target — the
// session-less one we just fixed, or a composer that is still `display: none` and cannot
// take focus. So both defer, and the poll additionally covers the case where the slot is not
// mounted AT ALL — a collapsed dock (the DS AppShell renders a dock's content only while it
// is open), which `navigate({kind:"view",id:"chat"})` un-collapses via `openView`. Bounded,
// so a chat slot that never produces a session (a fork surface holding the slot) cannot
// leave a timer looping.
import { chatStore, unusedSession } from "../chat/chat-store";
import type { ChatSession } from "../chat/chat-store";
import { prefillChatDraft, runSlashFromOutside, slashDispatchTarget } from "../chat/slashDispatch";
import type { PaletteCommand } from "../ext/paletteRegistry";
import { findSlashCommand, registeredSlashCommands } from "../ext/slashRegistry";
import type { SlashCommand } from "../lib/types";
import type { NavIntent } from "./palette/nav";

/** The palette's serializable navigation chokepoint (`navigate` in usePaletteRegistry).
 *  Injected rather than imported so this module stays free of the cycle — and so a test can
 *  watch what the rows ask for instead of mutating a real UI store. */
export type Navigate = (intent: NavIntent) => void;

/** What the palette does differently from the composer, for ONE client command. Every field
 *  is a decision made HERE, about a ROW — none of it belongs on `registerSlashCommand`,
 *  whose commands answer a different question ("did I claim this token?") than a row does
 *  ("would picking me visibly do what I say?"). It is one table rather than five keyed by
 *  the same names, so "why is `/btw` special?" is one line to read instead of five to
 *  cross-reference. A command with NO entry still gets a row — a fork's command lands on
 *  the optimistic defaults: runnable, needing at most A thread, searchable by its own
 *  description and usage. */
type RowSpec = {
  /** Acts on THIS chat — its accumulated content, or its per-tab mode — so with no session
   *  the row is DISABLED with the reason in `hint` rather than making a blank tab to run
   *  against. See the session-semantics note above. */
  needsThisChat?: true;
  /** The row PREFILLS `/<token> ` instead of dispatching, because the operator still has to
   *  supply something. Identical contract to a skill row, and it says the identical hint. */
  drafts?: true;
  /** A per-tab MODE this command toggles when dispatched bare — so the row DRAFTS (implied,
   *  no need to also set `drafts`) and its label states the mode's CURRENT value. Read the
   *  "why a mode must not be a one-Enter row" note above before removing either half: the
   *  draft is what keeps a permission off the one-Enter path, and the current value is what
   *  the row is actually worth opening the palette for. */
  mode?: (session: ChatSession) => "on" | "off";
  /** Dispatch THIS instead of the bare command name, where the row is NARROWER than the
   *  command it comes from. */
  token?: string;
  /** Use THIS in the label instead of the registry description, where that description LEADS
   *  with a branch this row does not run — a row must not lead with a verb it can't deliver. */
  blurb?: string;
  /** `registerKeybinding` id (ADR 0063) this row ADVERTISES — the host renders the LIVE combo
   *  through `effectiveCombo`, so a rebind can't leave the row lying. Only where the binding
   *  does the same thing as the command: `chat.new` is `chatStore.createSession`, `chat.clear`
   *  the same `requestClearSession` confirm `/clear` raises. `toDsCommand` renders
   *  `hint ?? combo`, so the combo shows only while the row has nothing more urgent to say —
   *  which is why the description rides the LABEL and `hintFor` returns undefined by default
   *  rather than filling that slot with prose. */
  keybinding?: string;
  /** Extra search terms: words an operator would reach for that the description doesn't
   *  contain ("wipe" for /clear, "llm" for /model, "yolo" for /bypass). Purely additive — the
   *  label (token AND description) and the `usage` string are searched already — so this
   *  never needs the drift guard a replacement-label table would. */
  find?: string[];
};

const ROWS: Record<string, RowSpec> = {
  new: { keybinding: "chat.new", find: ["start", "another", "conversation", "tab"] },
  clear: {
    needsThisChat: true,
    keybinding: "chat.clear",
    find: ["wipe", "reset", "erase", "empty", "delete history", "start over"],
  },
  export: { needsThisChat: true, find: ["download", "save", "transcript", "file", "markdown"] },
  publish: { needsThisChat: true, find: ["share", "link", "public", "read-only"] },
  // Dispatched bare, `/btw` posts its own "ask a side question after /btw" note and stops: it
  // needs a question, and a palette row has no way to ask for one. So it DRAFTS, landing the
  // operator in the composer with the token typed — exactly what picking it from the `/` menu
  // does.
  btw: {
    needsThisChat: true,
    drafts: true,
    find: ["aside", "side question", "by the way", "off the record"],
  },
  trajectory: {
    needsThisChat: true,
    find: ["timeline", "trace", "tool calls", "steps", "what the agent saw", "debug"],
  },
  prompt: { needsThisChat: true, find: ["system prompt", "instructions", "soul", "context"] },
  perf: {
    needsThisChat: true,
    find: ["performance", "latency", "cost", "tokens", "usage", "cache", "speed", "spend"],
  },
  compact: {
    needsThisChat: true,
    find: ["summarize", "condense", "shrink", "trim", "context window", "history"],
  },
  effort: { find: ["reasoning", "thinking", "low", "medium", "high", "max"] },
  model: { find: ["llm", "switch", "change", "provider", "favorites"] },
  // The two per-tab MODES. Both are `needsThisChat` because the row's job is to report the
  // current value, and both DRAFT because dispatching them bare would toggle — see the note
  // at the top of the file. `/bypass` is the one that matters: it arms `run_command`
  // auto-approval, and no fuzzy-matched palette row is allowed to do that in one keystroke.
  incognito: {
    needsThisChat: true,
    mode: (s) => (s.incognito ? "on" : "off"),
    find: ["private", "no memory", "forget", "ephemeral", "off the record"],
  },
  bypass: {
    needsThisChat: true,
    mode: (s) => (s.bypassPermissions ? "on" : "off"),
    find: ["permissions", "auto-approve", "approve", "yolo", "dangerous", "run_command"],
  },
  // `blurb` overrides the command's own description for the PALETTE row only, because the
  // description ("Show available commands & shortcuts") puts the word "shortcuts" into this
  // row's LABEL — and a label substring outranks a keyword, correctly. Settings' Keyboard
  // row claims "shortcuts" as a keyword (SECTION_KEYWORDS, #3291), so once #3292 put chat
  // rows in the same palette, /help won the word and typing "shortcuts" stopped opening the
  // Keyboard pane. The composer's /help description is unchanged; only this row reads
  // differently. `find` drops "shortcuts"/"keys" for the same reason — both are already
  // claimed — while "keyboard shortcuts" still lands here via "keyboard".
  help: { blurb: "Show the command reference", find: ["keyboard", "reference", "cheat sheet"] },
  // The only row NARROWER than its command, and the reason both overrides exist. The client
  // `/goal` claims ONLY the `new` subcommand (the guided form) and returns false for
  // everything else, so bare `/goal`, `/goal <text>` and `/goal clear` fall through to the
  // SERVER control command — dispatching bare "goal" from here would hit that false and do
  // nothing at all, the exact silent no-op this file is about. And the row can't wear the
  // registry description ("Set or check goals — /goal new opens a guided form"), which LEADS
  // with the two branches it does not run.
  goal: {
    token: "goal new",
    blurb: "Open the guided goal form",
    find: ["objective", "target", "self-driving", "form"],
  },
  // `/watch` keeps its description whole even though it names two branches, because the one
  // it LEADS with ("List watches") is the one the row runs — and the trailing clause teaches
  // the `/watch new` syntax the operator would otherwise have to already know.
  watch: {
    find: ["watches", "monitor", "notify", "remind", "alert", "condition", "trigger"],
  },
};

const HANDOFF_TRIES = 40;
const HANDOFF_MS = 16;

/** Wait out React's commit, then act. Covers all three deferred cases: a raised chat surface
 *  (one tick, so the composer is on screen before we focus it), a freshly created session,
 *  and a slot that is not mounted at all because its dock was collapsed — the raise
 *  un-collapses it and this poll waits for the remount. Bounded at
 *  ~`HANDOFF_TRIES * HANDOFF_MS`; a slot that never yields a session (a fork surface owning
 *  the chat slot) drops the action rather than looping forever. */
function whenSlotSettles(act: () => void, tries = HANDOFF_TRIES): void {
  setTimeout(() => {
    if (slashDispatchTarget()?.sessionId) {
      act();
      return;
    }
    if (tries > 0) whenSlotSettles(act, tries - 1);
  }, HANDOFF_MS);
}

/** Get the operator onto a visible chat with a session, then run `act` against the seam.
 *  The shared body of both row families — a client command's dispatch and a skill's draft
 *  prefill differ only in `act`, and need the identical staging.
 *
 *  There is deliberately NO early return on a null target. A null seam does NOT mean "this
 *  window has no chat": the DS AppShell UNMOUNTS a collapsed dock, so the ordinary "Hide left
 *  panel" gesture takes the slot — and its registration — away while chat is still very much
 *  this window's chat. That is also the state the palette is most useful in, so the row runs
 *  the same path it always did: raise (which `openView` un-collapses the dock for), make sure
 *  the store has a session, and poll until the remounted slot publishes its dispatcher.
 *  Whether this window has a chat to raise at all is decided ONCE, by the host, from
 *  `chatSlotProvider` — not per-run from a registration that comes and goes.
 *
 *  Raising goes through the injected `navigate`, never `useUI.getState()`: the frameless
 *  desktop launcher mounts this same registry in a shell-less JS context where a direct
 *  store mutation is an inert no-op, and only a serializable NavIntent crosses to the real
 *  window. (The launcher never gets these rows at all, but the rule is the rule, and a row
 *  that quietly bypassed it would be the next launcher bug.) */
function onChatReady(act: () => void, navigate: Navigate): void {
  const target = slashDispatchTarget();
  if (target?.sessionId && target.surfaceActive) {
    act(); // already looking at a live thread — nothing to wait for
    return;
  }
  // Hidden behind another surface, or unmounted with its dock — `openView` handles both
  // (it un-hides the surface, un-collapses its dock and makes it the active one).
  if (!target?.surfaceActive) navigate({ kind: "view", id: "chat" });
  // The STORE, not the seam, is the authority on whether a thread exists: with the dock
  // collapsed there is no seam to ask, and the remounted slot will adopt whatever the store
  // says is current. `createSession` reuses a pristine blank when there is one.
  if (!chatStore.getSnapshot().currentSessionId) chatStore.createSession();
  whenSlotSettles(act);
}

/** The one phrase for "this row leaves the send to you" — shared by every skill row, by
 *  every `drafts` client command and by both mode rows, so the operator learns it once. */
const DRAFT_HINT = "drafts in chat — you send it";

/** `/token · what it does` — the shape of the composer's own `/` menu, which is where the
 *  operator learned these (a `slash-name` span, then a `slash-desc` one). The description
 *  has to ride the LABEL rather than the hint because the hint is spent on the row's caveat
 *  exactly when the prose is most needed: with no chat open EVERY client row's hint is a
 *  reason, and a skill row's is always its draft promise — leaving a column of bare tokens
 *  (`/perf`, `/btw`, and skills named by whoever authored them: `/triage`, `/postmortem`)
 *  with nothing on the row saying what they are. The DS label span ellipsizes, so a long
 *  description degrades to its first clause instead of crowding the row's hint out. */
function rowLabel(token: string, blurb: string | undefined): string {
  return blurb ? `/${token} · ${blurb}` : `/${token}`;
}

/** The muted trailing text — the row's CAVEAT, in priority order: a disabled row explains
 *  ITSELF here (the seam's contract), a drafting row promises the draft, and a row that
 *  will make a tab first says so. Undefined otherwise, which is the point: the label
 *  already carries the description, so the slot is free for `toDsCommand` to render the
 *  live keybinding combo where a row advertises one. */
function hintFor(
  spec: RowSpec,
  opts: { disabledReason?: string; willOpenChat: boolean },
): string | undefined {
  if (opts.disabledReason) return opts.disabledReason;
  if (spec.drafts || spec.mode) return DRAFT_HINT;
  if (opts.willOpenChat) return "opens a chat first";
  return undefined;
}

/** Why this row can't act right now, or undefined if it can. Two reasons, both of which
 *  would otherwise be a row that looks live and does nothing:
 *    • it needs THIS chat and there isn't one;
 *    • it is `/new` and the pristine blank `createSession` would hand back IS the current
 *      tab, so the dispatch is a genuine no-op (chat-store.ts says so in as many words, and
 *      MobileShell/SessionSheet already disable their "+" on exactly this). */
function disabledReason(
  spec: RowSpec,
  name: string,
  state: { session: ChatSession | null; blankIsCurrent: boolean },
): string | undefined {
  if (spec.needsThisChat && !state.session) return "needs an open chat";
  if (name === "new" && state.blankIsCurrent) return "already on a blank chat";
  return undefined;
}

/** The rows for the client slash commands registered through `registerSlashCommand`. */
function clientRows(
  state: { session: ChatSession | null; blankIsCurrent: boolean },
  navigate: Navigate,
): PaletteCommand[] {
  return registeredSlashCommands().map((cmd) => {
    const spec: RowSpec = ROWS[cmd.name] ?? {};
    const token = spec.token ?? cmd.name;
    const reason = disabledReason(spec, cmd.name, state);
    // `/new` never promises to open a chat first — opening one IS the action. (It still
    // routes through `onChatReady`, which may have to raise a hidden surface; the create it
    // does on the way is harmless because `createSession` hands back the pristine blank the
    // dispatched `/new` would then ask for, rather than a second empty tab.)
    const willOpenChat = !state.session && !reason && cmd.name !== "new";
    // A mode row states the CURRENT value, which is the whole reason to list it — the label
    // is the only place outside the tab itself that says whether this chat is in incognito
    // or has approvals bypassed. Only computable with a session, which is why every mode is
    // `needsThisChat`.
    const mode = spec.mode && state.session ? spec.mode(state.session) : undefined;
    const blurb = spec.blurb ?? cmd.description;
    const drafts = !!spec.drafts || !!spec.mode;
    return {
      id: `chat-slash:${cmd.name}`,
      // The token LEADS the label: it is the name the operator already knows from the
      // composer and the one this audit found missing from ⌘⇧K. `usage` joins the keywords
      // so the argument words are searchable too ("incognito off", "effort max") — they are
      // what an operator types, and no other field carries them.
      label: rowLabel(token, mode ? `${blurb} — now ${mode}` : blurb),
      group: "Chat",
      keywords: ["slash", cmd.description, cmd.usage, ...(spec.find ?? [])].filter(
        Boolean,
      ) as string[],
      hint: hintFor(spec, { disabledReason: reason, willOpenChat }),
      keybinding: spec.keybinding,
      disabled: !!reason,
      // Carried, never evaluated here: the host resolves it per render, so a row gated on a
      // flag still in flight appears when `/api/flags` lands instead of being hidden for the
      // life of the window (ADR 0068's fail-closed window). `/publish` is the live case.
      flag: cmd.flag,
      run: (ctx) => {
        ctx.close();
        // Both returns are deliberately unread: by here the staging has already established
        // the two things a false would report (a registered slot, a live session), and the
        // host has already gated the row on its `flag` — so there is no fallback left to
        // take. Every OTHER caller of these must check.
        onChatReady(
          () => void (drafts ? prefillChatDraft(`/${token} `) : runSlashFromOutside(token)),
          navigate,
        );
      },
    };
  });
}

/** The rows for server user-facing SKILLS. Never an action row: a skill is applied by the
 *  server on the next send, so the row's whole promise is "you'll be in chat with this
 *  typed, ready to send". */
function skillRows(skills: SlashCommand[], navigate: Navigate): PaletteCommand[] {
  return skills
    .filter((c) => c.kind === "skill")
    // A client command owns the token if both exist — the composer's `/` menu dedups
    // client-first, so listing both here would offer a row that types a token something
    // else intercepts.
    .filter((skill) => !findSlashCommand(skill.name))
    .map((skill) => ({
      id: `chat-skill:${skill.name}`,
      // The description matters MORE here than on a client row: a skill's token is whatever
      // its author called it, so `/triage` alone names nothing an operator can act on.
      label: rowLabel(skill.name, skill.description || skill.usage),
      group: "Skills",
      keywords: ["skill", "playbook", "procedure", skill.description, skill.usage].filter(
        Boolean,
      ) as string[],
      // Says the quiet part: this row does not run anything. The operator still sends.
      hint: DRAFT_HINT,
      run: (ctx) => {
        ctx.close();
        // The trailing space is the caret affordance a picked `/`-menu row leaves too: most
        // skills take arguments, and the operator is one keystroke from typing them — or has
        // already typed them, since the seam prefixes rather than replacing the draft.
        onChatReady(() => prefillChatDraft(`/${skill.name} `), navigate);
      },
    }));
}

/** Everything a chat row RENDERS from, as one string.
 *
 *  The host `useSyncExternalStore`s this over `chatStore.subscribe` so the rows re-register
 *  when — and only when — one of them would look different. It has to be a projection rather
 *  than the snapshot itself: the chat store notifies on every streamed token, and
 *  re-registering the whole Chat group per frame would be a real cost for a list that
 *  changed in none of the ways it shows. */
export function chatPaletteSignature(): string {
  const state = chatStore.getSnapshot();
  const session = state.sessions.find((s) => s.id === state.currentSessionId) ?? null;
  const blank = unusedSession(state);
  return [
    session?.id ?? "",
    blank && blank.id === state.currentSessionId ? "blank" : "",
    session?.bypassPermissions ? "bypass" : "",
    session?.incognito ? "incognito" : "",
  ].join("|");
}

/** Every chat row this window should offer.
 *
 *  `reachable` is the host's answer to "can this window reach the BUILT-IN composer?" —
 *  `chatSlotProvider(...) === "builtin"`, decided from the surface/plugin registries rather
 *  than from whether a slot happens to be mounted. That distinction is the bug this
 *  parameter exists for: gating on `slashDispatchTarget()` looked equivalent and was not,
 *  because collapsing the dock chat lives on unmounts the slot and would have emptied the
 *  whole Chat + Skills group out of ⌘⇧K — in the state where the palette is most useful.
 *
 *  Everything else is read here: the chat store (session + per-tab modes) and the skill list
 *  the host holds from the shared `/api/chat/commands` query. */
export function chatSlashPaletteRows(
  navigate: Navigate,
  opts: { reachable: boolean; skills?: SlashCommand[] },
): PaletteCommand[] {
  // No built-in chat in this window: the frameless desktop launcher (which mounts no
  // ChatSurface at all) or a fork surface / plugin iframe holding the chat slot. Nothing
  // here could reach a composer, so offer nothing rather than a wall of dead rows.
  if (!opts.reachable) return [];
  const state = chatStore.getSnapshot();
  const session = state.sessions.find((s) => s.id === state.currentSessionId) ?? null;
  const blank = unusedSession(state);
  const rowState = {
    session,
    blankIsCurrent: !!blank && blank.id === state.currentSessionId,
  };
  return [...clientRows(rowState, navigate), ...skillRows(opts.skills ?? [], navigate)];
}
