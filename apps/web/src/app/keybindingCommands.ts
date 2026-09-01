// Keyboard actions as ⌘K rows (ADR 0061 × ADR 0063).
//
// The console registered 25 keybindings and the palette listed none of them, so ⌘K couldn't
// run the app's own commands and the shortcuts themselves were discoverable only by opening
// Settings ▸ Keyboard. These rows fix both: each one RUNS a registered binding's action and
// ADVERTISES the combo that binding currently answers to.
//
// The combo is never written down here. A row names the binding (`keybinding: <id>`), and the
// host renders `formatCombo(effectiveCombo(binding))` — the same call Settings ▸ Keyboard
// makes (KeybindingsPanel.tsx:199). Bindings are user-rebindable, so a literal "⌘K" in `hint`
// would start lying the moment the operator rebinds; going through the id keeps ⌘K and
// Settings ▸ Keyboard showing the same thing, always. (The other half of that loop is the
// `Settings: Keyboard` deep-link in usePaletteRegistry.ts: a palette that teaches you the
// chord has to offer the one screen that changes it.)
//
// ── Which bindings get a row ──────────────────────────────────────────────────────────
// An explicit ALLOW-LIST, not "everything in the registry". Two reasons: a generated row
// gets no keywords (the difference between a palette that answers "wipe conversation" and
// one that only answers to its own label), and sweeping the registry would conscript every
// FORK binding into the palette uninvited — a fork that wants a row registers one, the seam
// is right there. Every core id is triaged in the PR body; the drops in one line each:
//   • `palette.toggle` — a row that opens the thing you are looking at.
//   • `chat.stop` — Escape-to-stop, offered from an overlay Escape closes.
//   • `chat.tab.1…9` — nine ordinal jumps; chat tabs BY NAME supersede them.
//   • `focus.left` / `focus.right` / `focus.bottom` — `focusDock` bails when the column is
//     absent, and the DS AppShell renders `.pl-appshell__col--left` only while the dock is
//     OPEN (`{showLeft && <main …>}`). So the row is silently dead in exactly the state an
//     operator would reach for it from; `panel.toggle.*` is the row that serves that intent.
//   • `focus.chat` — a byte-identical duplicate of `composer.focus`'s run body; one action
//     should not be two rows.
//   • `settings.open` — the palette already has a "Settings" row doing the same thing; that
//     row now carries `keybinding: "settings.open"` instead, so it advertises ⌘, without a
//     twin (usePaletteRegistry.ts).
//
// ── Scope, and the surface an action actually needs ───────────────────────────────────
// `resolveBinding` (keybindings/resolve.ts) is the ONLY enforcement of `scope`, and a row
// runs `binding.run()` directly — so a chat-scoped action fired from the palette would run
// with focus in the overlay, which is never inside `[data-kb-scope="chat"]`. The answer is
// not to bypass the check but to make the precondition TRUE: a row carries the surface its
// binding's scope names, and `applyNavIntent` opens that surface before running the action.
// "Clear conversation" invoked from Knowledge means "go to chat and clear it" anyway. A
// binding whose scope this map can't resolve gets NO row — there is no honest way to run it.
//
// `scope` is not the whole answer, though, because it answers a different question: "where
// may this chord fire from", not "what has to be on screen for the action to do anything".
// `composer.focus` is GLOBAL (`/` works from Knowledge) yet the composer it focuses lives
// inside chat's dock, and a COLLAPSED dock is not in the DOM at all — the DS AppShell renders
// `{showLeft && <main …>}`. So a row may also name a `surface` outright; a resolvable scope
// still wins over it. Without that, "Focus chat composer" was the very dead row the drops
// below reject: its binding's own `setSurface("chat")` picks the active surface but never
// un-collapses the dock, never routes to the dock chat was MOVED to, and never sets
// `mobileActive` — all three of which `openView` does.
//
// ── Why no row is `disabled` ──────────────────────────────────────────────────────────
// The seam offers `disabled` + `hint` for a row that should stay listed and explain itself,
// and two of these actions can no-op: "Next chat tab" with one tab open, "Toggle latest tool
// block" with no tool block on screen. Neither gets it, because `disabled` is DATA read at
// registration and this module registers once at module load — the palette re-reads the
// registry on `flag` / `hostOnly` / keybinding-override changes, and on nothing else, so a
// state computed here would freeze at whatever was true when the bundle loaded and then lie
// in both directions. The seam's answer for a live condition is `registerPaletteSource`
// (re-read per keystroke), and core deliberately registers zero sources: the DS shows its
// "Searching…" affordance whenever ANY provider exists, so one would put a spinner in front
// of every keystroke in the default console to gate two rows. Both no-ops are also SELF-
// EVIDENT where the dropped `focus.*` rows' were not — there is visibly no other tab, and no
// tool block — so the row does nothing surprising by doing nothing.
import { registeredKeybindings } from "../ext/keybindingRegistry";
import { registerPaletteCommand } from "../ext/paletteRegistry";
import type { NavIntent } from "./usePaletteRegistry";
// Side effect: register the core defaults. This module reads each binding's `label` and
// `scope` at registration time, and nothing else on the palette's import path pulls
// coreKeybindings in (usePaletteRegistry takes only the registry + the combo/override
// helpers), so without this the rows would depend on which module happened to load first.
import "../keybindings/coreKeybindings";

/** A `data-kb-scope` id → the view id that owns it. The only way a scoped binding earns a
 *  palette row: opening that view is what makes its scope real. Core declares one scope
 *  ("chat", ChatSurface.tsx).
 *
 *  It governs the allow-list below and nothing else. A fork adding its OWN scoped binding
 *  registers its own row through `registerPaletteCommand` and never touches this; a fork
 *  that RE-SCOPES a core binding (`registerKeybinding` is last-write-wins by id) maps the
 *  new scope here too, and has to be registered by the time this module loads — the rows
 *  are built once, from whatever the registry holds then. */
const SCOPE_SURFACE: Record<string, string> = { chat: "chat" };

/** Words for the keyboard surface ITSELF, added to every row so typing any of them lists
 *  the whole family — the palette doubles as the shortcut cheat-sheet, and "Settings:
 *  Keyboard" carries them too so the way to REBIND lands in the same list.
 *
 *  PLURAL on purpose, and it is the rule for every keyword in this file. The matcher is
 *  `haystack.includes(term)` (the DS's `matchCommand`, mirrored by `matchesQuery` in
 *  usePaletteRegistry), so a keyword answers every query it CONTAINS: "shortcuts" answers
 *  both "shortcut" and "shortcuts", while the singular answers only itself — and "keyboard
 *  shortcuts" is what an operator actually types. Same reason "keys" is here next to
 *  "keyboard", and "keybindings" covers bind/binding/keybinding. */
export const SHORTCUT_KEYWORDS = ["keyboard", "shortcuts", "keys", "hotkeys", "keybindings"];

type KeybindingRow = {
  /** `registerKeybinding` id — the action the row runs, the shortcut it advertises, AND the
   *  wording it wears. There is deliberately NO per-row label override: an operator who
   *  found a chord on a ⌘K row has to be able to find the same name in Settings ▸ Keyboard
   *  to change it, and two spellings of one action is how that breaks. Same argument as the
   *  combo, one field over. */
  binding: string;
  /** Fuzzy-match terms. The label is already searched, so these are the words an operator
   *  reaches for INSTEAD of the label ("wipe", "sidebar", "drawer"). Anything there are MANY
   *  of goes in plural per the rule above ("conversations" answers "conversation" too); the
   *  one-per-side furniture stays singular, because nobody types "hide sidebars". */
  keywords: string[];
  /** The view this row's action needs ON SCREEN, for an action whose target lives on a
   *  surface its binding does not declare a `scope` for.
   *
   *  `scope` answers "where may this chord fire from", and a GLOBAL binding answers
   *  "anywhere" — correct for a chord, and not the same question as "what has to be mounted
   *  for the action to do anything". `composer.focus` is the case: global (`/` from any
   *  surface), but the composer it focuses exists only inside chat's dock, and a collapsed
   *  dock is not in the DOM at all. Its own body calls `setSurface("chat")`, which picks the
   *  active surface but does NOT un-collapse the dock, route to the dock chat was MOVED to,
   *  or set `mobileActive` — the three things `openView` does. Without this the row was a
   *  silent no-op from a collapsed dock: exactly the "dead in the state you'd reach for it
   *  from" failure that dropped `focus.left`/`focus.right`/`focus.bottom` above.
   *
   *  A resolvable `scope` still WINS over this (see `registerKeybindingCommands`): scope is a
   *  precondition the palette has to satisfy, this is only a hint about where the action's
   *  target lives. */
  surface?: string;
};

/** The allow-list, in display order. `chat.new` is also ADR 0057's never-built "New chat"
 *  core command — it is `chatStore.createSession()` + focus the composer, which is exactly
 *  this binding's run body, so it ships as this row rather than as a second one. */
export const KEYBINDING_ROWS: KeybindingRow[] = [
  {
    binding: "chat.new", // "New chat"
    keywords: ["new", "create", "start", "blank", "fresh", "chats", "conversations", "threads", "sessions", "tabs"],
  },
  {
    binding: "chat.clear", // "Clear conversation"
    // "conversations" even though the label carries the singular: the label answers only
    // "conversation", and "clear conversations" is the query an operator with tabs open types.
    keywords: ["clear", "wipe", "reset", "empty", "erase", "fresh",
               "chats", "conversations", "history", "messages", "transcripts"],
  },
  {
    binding: "composer.focus", // "Focus chat composer"
    // "reply" stays singular — "replies" doesn't contain it, so the plural rule can't apply.
    keywords: ["focus", "type", "write", "input", "messages", "prompts", "reply", "textbox", "chats"],
    // GLOBAL binding, chat-surface target — see `KeybindingRow.surface`. "I want to type a
    // message" is the intent an operator has precisely when chat is collapsed away.
    surface: "chat",
  },
  {
    binding: "chat.tab.next", // "Next chat tab"
    keywords: ["next", "forward", "cycle", "switch", "tabs", "chats", "sessions", "conversations"],
  },
  {
    binding: "chat.tab.prev", // "Previous chat tab"
    keywords: ["previous", "prev", "back", "backward", "cycle", "switch", "tabs", "chats", "sessions", "conversations"],
  },
  {
    binding: "chat.tool.toggle", // "Toggle latest tool block"
    // "chats" like its five chat-surface siblings — nothing in this label says where the
    // tool block it toggles actually lives.
    keywords: ["expand", "collapse", "show", "hide", "details",
               "chats", "tools", "calls", "blocks", "outputs", "results"],
  },
  {
    binding: "panel.toggle.left", // "Toggle left rail"
    keywords: ["sidebar", "panel", "dock", "show", "hide", "collapse", "expand", "close"],
  },
  {
    binding: "panel.toggle.right", // "Toggle right panel"
    keywords: ["sidebar", "rail", "dock", "inspector", "show", "hide", "collapse", "expand", "close"],
  },
  {
    binding: "panel.toggle.bottom", // "Toggle bottom dock"
    keywords: ["drawer", "tray", "panel", "show", "hide", "collapse", "expand", "close"],
  },
];

/** The palette id a binding's row takes — its own namespace, so it can't collide with the
 *  `open:` / `nav:` / `plug:` / `box:` families already in the registry. */
export function keybindingCommandId(bindingId: string): string {
  return `kb:${bindingId}`;
}

/**
 * Register the allow-list as ⌘K commands. `navigate` is the palette's NavIntent chokepoint
 * (usePaletteRegistry's module-private `navigate`), passed IN rather than imported so this
 * module has no runtime edge back to it — and so a test can watch the exact intent a row
 * emits. Every row goes through it: the frameless desktop launcher mounts this same registry
 * in a shell-less JS context where a direct store call is a silent no-op, so the intent has
 * to be able to cross to the main window.
 *
 * Returns one unregister for the whole set.
 */
export function registerKeybindingCommands(navigate: (intent: NavIntent) => void): () => void {
  const bindings = registeredKeybindings();
  const offs: (() => void)[] = [];
  for (const row of KEYBINDING_ROWS) {
    const binding = bindings.find((b) => b.id === row.binding);
    if (!binding) continue; // a fork dropped it — never ship a row whose action doesn't exist
    // Scope first: it is a PRECONDITION `applyNavIntent` has to make true, and a scope this
    // map can't resolve still gets no row (the fork contract in the header). A row's own
    // `surface` only fills in for a binding that declares no scope — a global chord whose
    // action nevertheless needs a surface mounted.
    const scoped = binding.scope ? SCOPE_SURFACE[binding.scope] : undefined;
    if (binding.scope && !scoped) continue; // unresolvable scope — see the header
    const surface = scoped ?? row.surface;
    offs.push(
      registerPaletteCommand({
        id: keybindingCommandId(binding.id),
        label: binding.label, // the BINDING's wording — see `KeybindingRow.binding`
        group: "Commands",
        keywords: [...row.keywords, ...SHORTCUT_KEYWORDS],
        // The row ADVERTISES the combo; it binds nothing. Resolved per render by the host
        // through `effectiveCombo`, so a Settings ▸ Keyboard rebind re-labels it.
        keybinding: binding.id,
        run: (ctx) => {
          navigate({ kind: "keybinding", id: binding.id, surface });
          ctx.close();
        },
      }),
    );
  }
  return () => {
    for (const off of offs) off();
  };
}
