// Keyboard actions as ⌘K rows (ADR 0061 × ADR 0063). Four things are worth pinning, and
// they are the four this file is organized around:
//   1. WHICH bindings became rows — the triage is a judgement call, so it belongs in a test
//      rather than only in a review comment: the 9 kept, and every deliberate drop named.
//   2. The advertised combo tracks a user OVERRIDE, not `defaultKeys` — the entire reason
//      the seam takes a binding ID instead of a literal "⌘K".
//   3. A SCOPED binding is handled, not bypassed: `resolveBinding` is the only enforcement
//      of `scope`, and a row calls `run()` directly — so the row must open the surface its
//      scope names first, and the action must genuinely land there.
//   4. A throwing binding leaves the palette working (and still closes it).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
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
import { applyNavIntent, usePaletteRegistry } from "./usePaletteRegistry";

// The live-combo case mounts the adapter and drives a store update through `act`.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Importing the adapter runs its module-load registrations — the core deep-links AND the
// keybinding rows, wired to the real (module-private) `navigate`. The generous timeout is
// for the transform: the adapter pulls in the DS palette, the UI store, react-query and the
// flags query, and that cold import blows past vitest's 5s default under a full-suite run.
beforeAll(async () => {
  await import("./usePaletteRegistry");
}, 30_000);

const paletteIds = () => registeredPaletteCommands("static").map((c) => c.id);
const row = (bindingId: string): PaletteCommand | undefined =>
  registeredPaletteCommands("static").find((c) => c.id === keybindingCommandId(bindingId));

describe("which keybindings became palette rows", () => {
  it("lists exactly the triaged allow-list", () => {
    const kbRows = paletteIds().filter((id) => id.startsWith("kb:"));
    expect(kbRows.sort()).toEqual(
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

  it("advertises its binding rather than a literal combo, and keeps them well-keyworded", () => {
    for (const r of KEYBINDING_ROWS) {
      const cmd = row(r.binding);
      expect(cmd?.keybinding).toBe(r.binding);
      expect(cmd?.hint).toBeUndefined(); // a literal here is what `keybinding` exists to prevent
      expect(cmd?.keywords).toEqual(expect.arrayContaining(["keyboard", "shortcut"]));
    }
  });

  it("gives the Settings deep-link ⌘, instead of a second 'Open Settings' row", () => {
    const settings = registeredPaletteCommands("static").find((c) => c.id === "settings");
    expect(settings?.keybinding).toBe("settings.open");
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
    expect(intents[0]).toMatchObject({ kind: "keybinding", id: "chat.clear", surface: "chat" });
    expect(intents[1].kind === "keybinding" && intents[1].surface).toBeUndefined();
  });

  it("closes the palette after dispatching", () => {
    expect(run("chat.tab.next")).toBe(true);
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
    expect(paletteIds().filter((id) => id.startsWith("kb:"))).toHaveLength(9);
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
