// Keyboard actions as ⌘K rows (ADR 0061 × ADR 0063). Five things are worth pinning, and
// they are the five this file is organized around:
//   1. WHICH bindings became rows — the triage is a judgement call, so it belongs in a test
//      rather than only in a review comment: the 9 kept, and every deliberate drop named.
//   2. That the rows are FINDABLE by the words an operator types. This is a search surface,
//      so a correct row nobody's query reaches is a row that doesn't ship.
//   3. The advertised combo tracks a user OVERRIDE, not `defaultKeys` — the entire reason
//      the seam takes a binding ID instead of a literal "⌘K".
//   4. A SCOPED binding is handled, not bypassed: `resolveBinding` is the only enforcement
//      of `scope`, and a row calls `run()` directly — so the row must open the surface its
//      scope names first, and the action must genuinely land there. A scope this module
//      can't resolve to a surface gets no row at all.
//   5. A throwing binding leaves the palette working (and still closes it).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Command, PaletteRegistry } from "@protolabsai/ui/command-palette";

import { chatStore } from "../chat/chat-store";
import { registerKeybinding, registeredKeybindings } from "../ext/keybindingRegistry";
import type { Keybinding } from "../ext/keybindingRegistry";
import { registeredPaletteCommands } from "../ext/paletteRegistry";
import type { PaletteCommand } from "../ext/paletteRegistry";
import { formatCombo } from "../keybindings/combo";
import { useKbIntents } from "../keybindings/intents";
import { effectiveCombo, useKeybindingOverrides } from "../keybindings/overrides";
import { runBindingById } from "../keybindings/useKeybindings";
import { useUI } from "../state/uiStore";
import { KEYBINDING_ROWS, keybindingCommandId, registerKeybindingCommands } from "./keybindingCommands";
import type { NavIntent } from "./usePaletteRegistry";
// Importing the adapter (for its exports, below) also RUNS its module-load registrations —
// the core deep-links and the keybinding rows, wired to the real, module-private `navigate`.
// That is what every assertion here reads, so the import is load-bearing beyond its bindings.
import { applyNavIntent, usePaletteRegistry } from "./usePaletteRegistry";

// The live-combo case mounts the adapter and drives a store update through `act`.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const paletteIds = () => registeredPaletteCommands("static").map((c) => c.id);
const kbRowIds = () => paletteIds().filter((id) => id.startsWith("kb:"));
const row = (bindingId: string): PaletteCommand | undefined =>
  registeredPaletteCommands("static").find((c) => c.id === keybindingCommandId(bindingId));

describe("which keybindings became palette rows", () => {
  it("lists exactly the triaged allow-list", () => {
    expect(kbRowIds().sort()).toEqual(
      [
        "kb:chat.clear",
        "kb:chat.new",
        "kb:chat.tab.next",
        "kb:chat.tab.prev",
        "kb:chat.tool.toggle",
        "kb:composer.focus",
        "kb:panel.toggle.bottom",
        "kb:panel.toggle.left",
        "kb:panel.toggle.right",
      ].sort(),
    );
  });

  it.each([
    ["palette.toggle", "a row that opens the thing you are already looking at"],
    ["chat.stop", "Escape-to-stop, offered from an overlay Escape closes"],
    ["settings.open", "the existing Settings row advertises it instead of a twin"],
    ["focus.chat", "byte-identical to composer.focus's run body"],
    ["focus.left", "focusDock no-ops when the dock is collapsed out of the DOM"],
    ["focus.right", "focusDock no-ops when the dock is collapsed out of the DOM"],
    ["focus.bottom", "focusDock no-ops when the dock is collapsed out of the DOM"],
  ])("drops %s — %s", (bindingId) => {
    expect(paletteIds()).not.toContain(keybindingCommandId(bindingId));
  });

  it("drops all nine chat.tab.N ordinal jumps (chat tabs BY NAME supersede them)", () => {
    for (let n = 1; n <= 9; n++) {
      expect(paletteIds()).not.toContain(keybindingCommandId(`chat.tab.${n}`));
    }
  });

  it("never ships a row whose binding isn't registered", () => {
    // A typo in the allow-list would otherwise ship a row that silently does nothing.
    const ids = new Set(registeredKeybindings().map((b) => b.id));
    for (const r of KEYBINDING_ROWS) expect(ids, `binding ${r.binding}`).toContain(r.binding);
  });

  it("advertises its binding rather than a literal combo", () => {
    for (const r of KEYBINDING_ROWS) {
      const cmd = row(r.binding);
      expect(cmd?.keybinding).toBe(r.binding);
      expect(cmd?.hint).toBeUndefined(); // a literal here is what `keybinding` exists to prevent
    }
  });

  it("wears the BINDING's wording, so Settings ▸ Keyboard names the same action the same way", () => {
    // The label half of the argument the combo makes: an operator who found a chord on a ⌘K
    // row goes to Settings ▸ Keyboard to change it, and a row worded differently there is a
    // row they can't find. There is no per-row label override for exactly this reason.
    const byId = new Map(registeredKeybindings().map((b) => [b.id, b.label]));
    for (const r of KEYBINDING_ROWS) {
      expect(byId.get(r.binding), r.binding).toBeTruthy(); // else the compare is vacuous
      expect(row(r.binding)?.label, r.binding).toBe(byId.get(r.binding));
    }
  });

  it("gives the Settings deep-link ⌘, instead of a second 'Open Settings' row", () => {
    const settings = registeredPaletteCommands("static").find((c) => c.id === "settings");
    expect(settings?.keybinding).toBe("settings.open");
  });

  it("offers the screen that REBINDS them, deep-linked to the Keyboard section", () => {
    const cmd = registeredPaletteCommands("static").find((c) => c.id === "box:keybindings");
    expect(cmd, "Settings: Keyboard row").toBeTruthy();
    cmd!.run({ close: () => {} });
    // The id of the settings section table's Keyboard entry (`id: "keybindings"`, under
    // `settings/`) — a typo here is a dialog that opens on whatever section was last used,
    // which looks exactly like the deep-link working.
    expect(useUI.getState().globalSettingsSection).toBe("keybindings");
    useUI.getState().closeGlobalSettings();
  });
});

// ── Findability ──────────────────────────────────────────────────────────────────────
// The palette is a SEARCH surface: a row whose keywords miss the words an operator types is
// as good as unregistered, and that failure is invisible to every other test in this file.
//
// `finds` mirrors the DS's `matchCommand` (command-palette.views.tsx): lowercase the row's
// label + group + keywords into one haystack, and require every whitespace-separated term to
// appear in it — `haystack.includes(term)`, which is why the keyword lists prefer the plural
// ("shortcuts" answers "shortcut" too; "shortcut" does not answer "shortcuts"). Conservative
// by one field: the DS also searches the row's rendered `hint` (its combo), which this can't
// see, so anything passing here passes there.
describe("an operator can actually find these rows", () => {
  const finds = (query: string, cmd: PaletteCommand | undefined): boolean => {
    if (!cmd) return false;
    const hay = [cmd.label, cmd.group, ...(cmd.keywords ?? [])].join(" ").toLowerCase();
    return query
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .every((term) => hay.includes(term));
  };

  it.each([
    ["new chat", "chat.new"],
    ["start conversation", "chat.new"],
    ["wipe conversation", "chat.clear"],
    ["clear messages", "chat.clear"],
    ["focus composer", "composer.focus"],
    ["write reply", "composer.focus"],
    ["next tab", "chat.tab.next"],
    ["switch tabs", "chat.tab.prev"],
    ["expand tool output", "chat.tool.toggle"],
    ["tool results", "chat.tool.toggle"],
    ["clear conversations", "chat.clear"],
    ["hide sidebar", "panel.toggle.left"],
    ["right inspector", "panel.toggle.right"],
    ["collapse drawer", "panel.toggle.bottom"],
  ])("'%s' finds %s", (query, bindingId) => {
    expect(finds(query, row(bindingId))).toBe(true);
  });

  it("answers BOTH numbers of every countable word it claims", () => {
    // `haystack.includes(term)` means a keyword answers only the queries it CONTAINS:
    // "conversations" answers "conversation" too, the singular answers only itself. So a
    // countable word stored singular silently drops half the queries for it — invisibly,
    // because the row is still registered and every other test in this file still passes.
    // Pinned as BEHAVIOR rather than as a spelling rule, so a row stays free to carry both
    // forms instead.
    //
    // The list is the words these rows use for things there are MANY of. Deliberately not in
    // it: `panel` / `dock` / `sidebar` / `drawer` / `rail` / `tray`, of which there is one
    // per side — "hide sidebars" is not a query anyone types — and `reply`, whose plural
    // does not contain it, so no single form can answer both.
    const countable = ["chat", "conversation", "session", "tab", "thread", "message",
      "transcript", "prompt", "tool", "call", "result", "output", "block", "key", "shortcut",
      "hotkey", "keybinding"];
    for (const r of KEYBINDING_ROWS) {
      const cmd = row(r.binding);
      const hay = [cmd?.label, ...(cmd?.keywords ?? [])].join(" ").toLowerCase();
      for (const word of countable) {
        if (!hay.includes(word)) continue; // this row doesn't claim the word at all
        expect(finds(word, cmd), `${r.binding} ← "${word}"`).toBe(true);
        expect(finds(`${word}s`, cmd), `${r.binding} ← "${word}s"`).toBe(true);
      }
    }
  });

  it("lists the WHOLE keyboard surface — plus the way to rebind it — for 'keyboard shortcuts'", () => {
    // The claim `keybindingCommands.ts` makes: ⌘K doubles as the shortcut cheat-sheet. The
    // plural is what an operator types, and the form the first draft of these keywords
    // missed — with a singular tail this case is the only one in the file that reddens.
    for (const r of KEYBINDING_ROWS) {
      expect(finds("keyboard shortcuts", row(r.binding)), `row for ${r.binding}`).toBe(true);
    }
    const rebind = registeredPaletteCommands("static").find((c) => c.id === "box:keybindings");
    expect(finds("keyboard shortcuts", rebind)).toBe(true);
    expect(finds("rebind shortcut", rebind)).toBe(true);
  });
});

// ── The intent each row emits ────────────────────────────────────────────────────────
// Re-registered against a spy so the exact NavIntent is observable. Last-write-wins by id
// replaces the production rows for the duration; `off()` withdraws them again.
describe("every row routes through the NavIntent chokepoint", () => {
  const intents: NavIntent[] = [];
  let off = () => {};

  beforeEach(() => {
    intents.length = 0;
    off = registerKeybindingCommands((intent) => intents.push(intent));
  });
  afterEach(() => {
    // `off()` genuinely REMOVES these ids (last-write-wins made the spy rows the registered
    // ones), so the module-load rows are gone until something re-registers them. Re-register
    // against `applyNavIntent` — the sink `navigate` defaults to — so the later cases run
    // against rows that behave like the shipped ones.
    off();
    registerKeybindingCommands(applyNavIntent);
  });

  const run = (bindingId: string) => {
    const cmd = row(bindingId);
    expect(cmd, `row for ${bindingId}`).toBeTruthy();
    let closed = false;
    cmd!.run({ close: () => (closed = true) });
    return closed;
  };

  it("names the binding — never a store call the launcher would swallow", () => {
    run("panel.toggle.left");
    expect(intents).toEqual([{ kind: "keybinding", id: "panel.toggle.left", surface: undefined }]);
  });

  it("carries the surface a SCOPED binding's scope names, and none for a global one", () => {
    run("chat.clear");
    run("composer.focus");
    expect(intents[0]).toEqual({ kind: "keybinding", id: "chat.clear", surface: "chat" });
    expect(intents[1]).toEqual({ kind: "keybinding", id: "composer.focus", surface: undefined });
  });

  it("closes the palette after dispatching", () => {
    expect(run("chat.tab.next")).toBe(true);
  });
});

// ── The fork contract ────────────────────────────────────────────────────────────────
describe("a scope with no surface gets no row at all", () => {
  it("skips the binding and ships the other eight", () => {
    const original = registeredKeybindings().find((b) => b.id === "chat.clear")!;
    // Claim the nine ids off the module-load registration, then withdraw OUR set: the seam is
    // last-write-wins, so this is the only way to get to a genuinely empty `kb:` namespace.
    registerKeybindingCommands(() => {})();
    expect(kbRowIds()).toHaveLength(0);
    // A fork adding a `data-kb-scope` its own panel declares, without teaching SCOPE_SURFACE
    // about it: `applyNavIntent` could not make the precondition true, so there is no honest
    // row to build — and building one anyway is the dead-row failure the triage rejected.
    registerKeybinding({ ...original, scope: "a-fork-scope-nobody-mapped" });
    const off = registerKeybindingCommands(() => {});
    try {
      expect(kbRowIds()).not.toContain(keybindingCommandId("chat.clear"));
      expect(kbRowIds()).toHaveLength(8);
    } finally {
      off();
      registerKeybinding(original);
      registerKeybindingCommands(applyNavIntent); // restore the shipped rows for what follows
    }
  });
});

// ── Applying the intent ──────────────────────────────────────────────────────────────
describe("applyNavIntent runs the action with its scope made real", () => {
  afterEach(() => {
    for (const s of chatStore.getSnapshot().sessions) chatStore.deleteSession(s.id);
    chatStore.clearClearRequest();
  });

  it("opens the scoped binding's surface BEFORE running it", () => {
    useUI.setState({ surface: "knowledge" });
    const session = chatStore.createSession();

    applyNavIntent({ kind: "keybinding", id: "chat.clear", surface: "chat" });

    // Navigated (so the chat-scoped action is no longer firing at a surface nobody is on)…
    expect(useUI.getState().surface).toBe("chat");
    // …and the action actually landed: chat.clear parks a request for the confirm dialog.
    expect(chatStore.getSnapshot().pendingClearRequest).toBe(session.id);
  });

  it("runs 'New chat' — a new session plus a composer-focus request (ADR 0057)", () => {
    useUI.setState({ surface: "knowledge" });
    const before = chatStore.getSnapshot().sessions.length;
    const nonce = useKbIntents.getState().composerFocusNonce;

    applyNavIntent({ kind: "keybinding", id: "chat.new", surface: "chat" });

    expect(chatStore.getSnapshot().sessions.length).toBe(before + 1);
    expect(useKbIntents.getState().composerFocusNonce).toBe(nonce + 1);
    expect(useUI.getState().surface).toBe("chat");
  });

  it("is a no-op for an id nothing registered", () => {
    expect(runBindingById("nope.not.a.binding")).toBe(false);
    expect(() => applyNavIntent({ kind: "keybinding", id: "nope.not.a.binding" })).not.toThrow();
  });
});

// ── A throwing binding ───────────────────────────────────────────────────────────────
describe("a throwing binding does not break the palette", () => {
  let original: Keybinding | undefined;

  beforeEach(() => {
    original = registeredKeybindings().find((b) => b.id === "chat.clear");
    // `registerKeybinding` is last-write-wins by id, so this replaces the real action for
    // the duration — the palette row (which resolves by id at RUN time) now points at it.
    registerKeybinding({ ...original!, run: () => { throw new Error("boom"); } });
  });
  afterEach(() => {
    if (original) registerKeybinding(original);
  });

  it("swallows the throw and still closes the palette", () => {
    const cmd = row("chat.clear");
    let closed = false;
    expect(() => cmd!.run({ close: () => (closed = true) })).not.toThrow();
    expect(closed).toBe(true);
    // …and the registry the palette reads is intact, so the next open still lists everything.
    expect(kbRowIds()).toHaveLength(9);
  });
});

// ── The live combo ───────────────────────────────────────────────────────────────────
// Mounted rather than unit-tested against `toDsCommand`, because the claim is end-to-end:
// rebinding in Settings ▸ Keyboard must RE-LABEL the row, which needs the adapter's
// `kbOverrides` dependency to re-run the registration effect. Testing the mapper alone
// would pass with that dependency deleted.
describe("the advertised combo is the LIVE one, not defaultKeys", () => {
  let root: Root | null = null;

  beforeEach(() => {
    // The roster poll would otherwise hit the network on every mount; hanging it keeps the
    // fleet data undefined, a state the hook already handles (`fleet?.agents ?? []`).
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
  });
  afterEach(() => {
    useKeybindingOverrides.getState().resetAll();
    if (root) act(() => root!.unmount());
    root = null;
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  async function mountRegistry(): Promise<PaletteRegistry> {
    let registry: PaletteRegistry | null = null;
    const Probe = () => {
      registry = usePaletteRegistry([], []);
      return null;
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    await act(async () => {
      root!.render(h(QueryClientProvider, { client }, h(Probe)));
    });
    await vi.waitFor(() => expect(registry).not.toBeNull());
    return registry!;
  }

  const hintOf = (registry: PaletteRegistry, bindingId: string) =>
    (registry.getStaticCommands() as Command[]).find((c) => c.id === keybindingCommandId(bindingId))
      ?.hint;

  it("renders the default combo, then the OVERRIDE once the operator rebinds", async () => {
    const binding = registeredKeybindings().find((b) => b.id === "chat.clear")!;
    const registry = await mountRegistry();

    // Platform-agnostic on purpose: `formatCombo` renders ⌘ on mac and Ctrl elsewhere, and
    // jsdom is neither reliably. What matters is WHICH combo string is formatted.
    expect(hintOf(registry, "chat.clear")).toBe(formatCombo(binding.defaultKeys));

    await act(async () => {
      useKeybindingOverrides.getState().setBinding("chat.clear", "mod+shift+x");
    });

    const rebound = hintOf(registry, "chat.clear");
    expect(rebound).toBe(formatCombo("mod+shift+x"));
    expect(rebound).toBe(formatCombo(effectiveCombo(binding))); // …i.e. what Settings shows
    expect(rebound).not.toBe(formatCombo(binding.defaultKeys)); // the literal would lie here
  });
});
