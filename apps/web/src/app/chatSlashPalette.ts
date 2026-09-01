// The chat's own verbs in ⌘⇧K (#3292) — the 16 client slash commands (ADR 0061) and every
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
// skill drafts; one client command does too (`/btw`, which needs a question the palette has
// no way to ask for — running it bare would only print its own usage note).
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
// ── Why a dynamic SOURCE and not static registration ────────────────────────────────
// Both halves are live, for different reasons, and a snapshot would freeze each:
//   • the skill list is LIVE server state — `/api/chat/commands` re-resolves the registries
//     per request (`_operator_chat_commands`), so enabling a plugin or authoring a skill
//     changes it with NO restart. We read it from the shared React-Query CACHE
//     (`queryKeys.chatCommands`, populated by the chat surface's own `chatCommandsQuery`)
//     rather than fetching: a source runs on EVERY keystroke into the palette, so it must
//     be cheap and synchronous — see `chatSlashPaletteRows`.
//   • the client command list is fixed at module load, but the rows' DISABLED state is not:
//     it depends on whether the visible chat slot currently has a session, which changes as
//     the operator opens and closes tabs. A static row snapshotted `disabled: true` at boot
//     would stay dead for the life of the window.
// Core shipping a source is a real (small) cost: the DS commands view shows its "Searching…"
// spinner and debounces 120ms whenever ANY provider declares `getCommands`, so these rows
// land a beat after the statics on each keystroke. That is the price of rows that can't lie.
//
// ── SESSION SEMANTICS — decided per command, not guessed ─────────────────────────────
// 13 of the 16 client commands `return false` without a session and two more answer through
// a `noteToThread` that is itself a no-op without one (slashDispatch.ts spells out the
// inventory; coreSlashCommands.test.ts pins it). In the COMPOSER false means "fall through
// to the draft" — a visible outcome. From the palette there is no draft to fall through to,
// so the same false is a row that visibly does NOTHING. Every command therefore lands in
// one of three buckets, on ONE question: does it need THIS conversation's content, or
// merely A thread?
//
//   NEEDS THIS CONVERSATION → the row is DISABLED with the reason in `hint`.
//     /clear /export /publish /btw /trajectory /prompt /perf /compact
//     Each reads or rewrites accumulated history. Auto-creating a blank tab to run them
//     would be theater: exporting an empty transcript or compacting nothing "succeeds" and
//     tells the operator nothing. Disabled-and-explaining beats hidden (the row stays
//     discoverable) and beats a dead-looking runnable row.
//
//   NEEDS ONLY A THREAD → the row CREATES OR SWITCHES to one first, then dispatches.
//     /help /effort /model /incognito /bypass /goal /watch
//     Two shapes, same requirement: a place to print (`/help`, `/watch`, `/goal`'s form) or
//     a tab to configure before you type into it (`/effort`, `/model`, `/incognito`,
//     `/bypass`). A fresh tab is a perfectly good answer to both — and `chatStore.createSession`
//     hands back a pristine blank instead of piling one up, so this is "focus the empty tab
//     you already have" far more often than it is "make a new one".
//
//   NEEDS NOTHING → `/new`, which never reads `sessionId`.
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
// take focus. So both defer, and the create case additionally polls until the new slot has
// published a session. Bounded, so a chat slot that never produces one (a fork surface
// holding the slot) cannot leave a timer looping.
import { chatStore } from "../chat/chat-store";
import { prefillChatDraft, runSlashFromOutside, slashDispatchTarget } from "../chat/slashDispatch";
import { registerPaletteSource } from "../ext/paletteRegistry";
import type { PaletteCommand } from "../ext/paletteRegistry";
import { findSlashCommand, registeredSlashCommands } from "../ext/slashRegistry";
import { queryClient } from "../lib/queryClient";
import { chatCommandsQuery } from "../lib/queries";
import type { NavIntent } from "./usePaletteRegistry";

/** The palette's serializable navigation chokepoint (`navigate` in usePaletteRegistry).
 *  Injected rather than imported so this module stays free of the cycle — and so a test can
 *  watch what the rows ask for instead of mutating a real UI store. */
export type Navigate = (intent: NavIntent) => void;

/** What the palette does differently from the composer, for ONE client command. Every field
 *  is a decision made HERE, about a ROW — none of it belongs on `registerSlashCommand`,
 *  whose commands answer a different question ("did I claim this token?") than a row does
 *  ("would picking me visibly do what I say?"). It is one table rather than five keyed by
 *  the same sixteen names, so "why is `/btw` special?" is one line to read instead of five
 *  to cross-reference. A command with NO entry still gets a row — a fork's command lands on
 *  the optimistic defaults: runnable, needing at most A thread, searchable by its own
 *  description and usage. */
type RowSpec = {
  /** Acts on THIS conversation's accumulated content, so with no session the row is DISABLED
   *  with the reason in `hint` rather than making a blank tab to run against — see the
   *  session-semantics note above. */
  needsThisChat?: true;
  /** The row PREFILLS `/<token> ` instead of dispatching, because the operator still has to
   *  supply something. Identical contract to a skill row, and it says the identical hint. */
  drafts?: true;
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
  incognito: { find: ["private", "no memory", "forget", "ephemeral", "off the record"] },
  help: { find: ["shortcuts", "keyboard", "reference", "keys", "cheat sheet"] },
  bypass: { find: ["permissions", "auto-approve", "approve", "yolo", "dangerous", "run_command"] },
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

const HANDOFF_TRIES = 30;
const HANDOFF_MS = 16;

/** Wait out React's commit, then act. Covers BOTH deferred cases: a raised chat surface
 *  (one tick, so the composer is on screen before we focus it) and a freshly created
 *  session (poll until the new slot has published its dispatcher). Bounded at
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
 *  Raising goes through the injected `navigate`, never `useUI.getState()`: the frameless
 *  desktop launcher mounts this same registry in a shell-less JS context where a direct
 *  store mutation is an inert no-op, and only a serializable NavIntent crosses to the real
 *  window. (The launcher never gets these rows at all — no chat slot is mounted there, so
 *  `slashDispatchTarget()` is null and the source returns nothing — but the rule is the
 *  rule, and a row that quietly bypassed it would be the next launcher bug.) */
function onChatReady(act: () => void, navigate: Navigate): void {
  const target = slashDispatchTarget();
  if (!target) return; // no chat slot in this window — the source offered no rows anyway
  if (!target.surfaceActive) navigate({ kind: "view", id: "chat" });
  if (target.sessionId && target.surfaceActive) {
    act(); // already looking at a live thread — nothing to wait for
    return;
  }
  if (!target.sessionId) chatStore.createSession(); // reuses a pristine blank when there is one
  whenSlotSettles(act);
}

/** The one phrase for "this row leaves the send to you" — shared by every skill row and by
 *  every `drafts` client command, so the operator learns it once. */
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
  opts: { disabled: boolean; willOpenChat: boolean },
): string | undefined {
  if (opts.disabled) return "needs an open chat";
  if (spec.drafts) return DRAFT_HINT;
  if (opts.willOpenChat) return "opens a chat first";
  return undefined;
}

/** The rows for the client slash commands registered through `registerSlashCommand`. */
function clientRows(sessionId: string | null, navigate: Navigate): PaletteCommand[] {
  return registeredSlashCommands().map((cmd) => {
    const spec: RowSpec = ROWS[cmd.name] ?? {};
    const token = spec.token ?? cmd.name;
    const disabled = !sessionId && !!spec.needsThisChat;
    // `/new` never promises to open a chat first — opening one IS the action. (It still
    // routes through `onChatReady`, which may have to raise a hidden surface; the create it
    // does on the way is harmless because `createSession` hands back the pristine blank the
    // dispatched `/new` would then ask for, rather than a second empty tab.)
    const willOpenChat = !sessionId && !disabled && cmd.name !== "new";
    return {
      id: `chat-slash:${cmd.name}`,
      // The token LEADS the label: it is the name the operator already knows from the
      // composer and the one this audit found missing from ⌘⇧K. `usage` joins the keywords
      // so the argument words are searchable too ("incognito off", "effort max") — they are
      // what an operator types, and no other field carries them.
      label: rowLabel(token, spec.blurb ?? cmd.description),
      group: "Chat",
      keywords: ["slash", cmd.description, cmd.usage, ...(spec.find ?? [])].filter(
        Boolean,
      ) as string[],
      hint: hintFor(spec, { disabled, willOpenChat }),
      keybinding: spec.keybinding,
      disabled,
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
          () => void (spec.drafts ? prefillChatDraft(`/${token} `) : runSlashFromOutside(token)),
          navigate,
        );
      },
    };
  });
}

/** The rows for server user-facing SKILLS. Never an action row: a skill is applied by the
 *  server on the next send, so the row's whole promise is "you'll be in chat with this
 *  typed, ready to send". */
function skillRows(navigate: Navigate): PaletteCommand[] {
  // Read through the SHARED query's own key (`chatCommandsQuery`, the one the chat surface
  // fetches with) rather than naming the key here: the key and the row type then can't drift
  // apart, and `getQueryData` infers the response shape from it. A cache read, never a
  // fetch — see `chatSlashPaletteRows`.
  const cached = queryClient.getQueryData(chatCommandsQuery().queryKey);
  const skills = (cached?.commands ?? []).filter((c) => c.kind === "skill");
  return skills
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

/** Every chat row this window should offer, computed fresh. Called on palette open and on
 *  EVERY keystroke, so it stays cheap and synchronous: three module reads (the client
 *  registry array, the seam's projection, one React-Query CACHE lookup) and a map. It must
 *  never fetch — `chatCommandsQuery` is owned by the chat surface, and a fetch here would
 *  fire one per character typed. */
export function chatSlashPaletteRows(navigate: Navigate): PaletteCommand[] {
  const target = slashDispatchTarget();
  // No chat slot in this window: the frameless desktop launcher (which mounts no
  // ChatSurface) or a fork surface / plugin iframe holding the chat slot. Nothing here can
  // reach a composer, so offer nothing rather than a wall of dead rows.
  if (!target) return [];
  return [...clientRows(target.sessionId, navigate), ...skillRows(navigate)];
}

/** Register the source. Called once at module load by usePaletteRegistry, which owns
 *  `navigate`; returns an unregister so a test (or a fork replacing the rows) can withdraw
 *  it. */
export function registerChatSlashPalette(navigate: Navigate): () => void {
  return registerPaletteSource(() => chatSlashPaletteRows(navigate));
}
