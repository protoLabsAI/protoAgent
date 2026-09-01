// Keyboard actions as ⌘K rows (ADR 0061 × ADR 0063).
//
// The console registers 25 keybindings and the palette listed none of them, so ⌘K couldn't
// run the app's own commands and the shortcuts themselves were discoverable only by opening
// Settings ▸ Keyboard. These rows fix both: each one RUNS a registered binding's action and
// ADVERTISES the combo that binding currently answers to.
//
// The combo is never written down here. A row names the binding (`keybinding: <id>`), and the
// host renders `formatCombo(effectiveCombo(binding))` — the same call Settings ▸ Keyboard
// makes (KeybindingsPanel.tsx:199). Bindings are user-rebindable, so a literal "⌘K" in `hint`
// would start lying the moment the operator rebinds; going through the id keeps ⌘K and
// Settings ▸ Keyboard showing the same thing, always.
//
// ── Which bindings get a row ──────────────────────────────────────────────────────────
// An explicit ALLOW-LIST, not "everything in the registry". Two reasons: a generated row
// gets no keywords (the difference between a palette that finds "wipe the chat" and one that
// only answers to its own label), and sweeping the registry would conscript every FORK
// binding into the palette uninvited — a fork that wants a row registers one, the seam is
// right there. Every core id is triaged in the PR body; the drops in one line each:
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
// ── Scope ─────────────────────────────────────────────────────────────────────────────
// `resolveBinding` (keybindings/resolve.ts) is the ONLY enforcement of `scope`, and a row
// runs `binding.run()` directly — so a chat-scoped action fired from the palette would run
// with focus in the overlay, which is never inside `[data-kb-scope="chat"]`. The answer is
// not to bypass the check but to make the precondition TRUE: a row carries the surface its
// binding's scope names, and `applyNavIntent` opens that surface before running the action.
// "Clear conversation" invoked from Knowledge means "go to chat and clear it" anyway. A
// binding whose scope this map can't resolve gets NO row — there is no honest way to run it.
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
 *  ("chat", ChatSurface.tsx). A fork adding a scope adds its surface here. */
const SCOPE_SURFACE: Record<string, string> = { chat: "chat" };

type KeybindingRow = {
  /** `registerKeybinding` id — the action the row runs AND the shortcut it advertises. */
  binding: string;
  /** Fuzzy-match terms. The label is already searched, so these are the words an operator
   *  reaches for INSTEAD of the label ("wipe", "sidebar", "conversation"). */
  keywords: string[];
  /** Palette wording, when it should differ from the Settings ▸ Keyboard label. */
  label?: string;
};

/** The allow-list, in display order. `chat.new` is also ADR 0057's never-built "New chat"
 *  core command — it is `chatStore.createSession()` + focus the composer, which is exactly
 *  this binding's run body, so it ships as this row rather than as a second one. */
export const KEYBINDING_ROWS: KeybindingRow[] = [
  {
    binding: "chat.new",
    keywords: ["new", "create", "start", "blank", "chat", "conversation", "thread", "session", "tab"],
  },
  {
    binding: "chat.clear",
    keywords: ["clear", "wipe", "reset", "empty", "erase", "conversation", "chat", "history", "messages", "fresh"],
  },
  {
    binding: "composer.focus",
    keywords: ["focus", "composer", "compose", "type", "write", "input", "message", "prompt", "reply", "chat"],
  },
  {
    binding: "chat.tab.next",
    keywords: ["next", "forward", "cycle", "switch", "chat", "tab", "session", "conversation"],
  },
  {
    binding: "chat.tab.prev",
    keywords: ["previous", "prev", "back", "cycle", "switch", "chat", "tab", "session", "conversation"],
  },
  {
    binding: "chat.tool.toggle",
    keywords: ["toggle", "expand", "collapse", "tool", "call", "block", "output", "result", "details", "latest"],
  },
  {
    binding: "panel.toggle.left",
    keywords: ["toggle", "left", "rail", "sidebar", "panel", "dock", "show", "hide", "collapse", "expand"],
  },
  {
    binding: "panel.toggle.right",
    keywords: ["toggle", "right", "panel", "sidebar", "dock", "inspector", "show", "hide", "collapse", "expand"],
  },
  {
    binding: "panel.toggle.bottom",
    keywords: ["toggle", "bottom", "dock", "panel", "drawer", "tray", "show", "hide", "collapse", "expand"],
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
    const surface = binding.scope ? SCOPE_SURFACE[binding.scope] : undefined;
    if (binding.scope && !surface) continue; // unresolvable scope — see the header
    offs.push(
      registerPaletteCommand({
        id: keybindingCommandId(binding.id),
        label: row.label ?? binding.label,
        group: "Commands",
        // "keyboard"/"shortcut" on every row so typing either lists the whole keyboard
        // surface — the palette doubles as the shortcut cheat-sheet.
        keywords: [...row.keywords, "keyboard", "shortcut", "key"],
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
