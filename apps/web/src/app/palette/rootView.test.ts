// The host-owned palette root view. jsdom + react-dom/client (the console has no
// @testing-library, and vitest's include glob is `src/**/*.test.ts` with no tsx
// alternation — a `.test.tsx` file is SILENTLY never collected — so elements are built with
// React.createElement; same pattern as chat/clearConfirmDialog.test.ts).
//
// These tests exist because owning the root view means owning everything `commandsView`
// did, and every one of those inherited behaviours fails SILENTLY: the provider loop going
// missing makes a future live-search PR land green and do nothing; a first commit that
// resolves the DS default instead of ours looks like a flicker; a stranded selection runs
// the wrong command on Enter.
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CommandPalette, createPaletteRegistry } from "@protolabsai/ui/command-palette";
import type { Command, PaletteRegistry } from "@protolabsai/ui/command-palette";

import { createRankedPaletteRegistry } from "./registry";
import { RECENT_GROUP, emptyQueryList, paletteRootView } from "./rootView";
import type { RecentMap } from "./recents";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
// jsdom implements no layout, so it ships no `scrollIntoView` — the DS's own commands view
// calls it too, so this is an environment gap, not a behaviour under test.
Element.prototype.scrollIntoView ??= () => {};

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.body.innerHTML = "";
  vi.useRealTimers();
});

const cmd = (c: Partial<Command> & { id: string; label: string }): Command => ({
  run: () => {},
  ...c,
});

/** Mount the DS palette (open) over a registry — the real host, not a stand-in, because
 *  "does the DS resolve OUR view?" is the thing under test. */
function mountPalette(registry: PaletteRegistry) {
  act(() => {
    root.render(
      h(CommandPalette, { open: true, onOpenChange: () => {}, registry }),
    );
  });
}

const rows = () => [...document.querySelectorAll<HTMLElement>('[role="option"]')];
const labels = () => rows().map((r) => r.querySelector(".pl-cmdk-commands__label")?.textContent ?? "");
const input = () => document.querySelector<HTMLInputElement>(".pl-cmdk-commands__input")!;
const selected = () => document.querySelector<HTMLElement>('[data-sel="true"]')?.textContent ?? null;

function type(value: string) {
  act(() => {
    const el = input();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function press(key: string) {
  act(() => {
    input().dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
  });
}

describe("the DS hands the root over — registration at CONSTRUCTION", () => {
  it("puts id 'commands' in getViews() synchronously, before any render", () => {
    const registry = createRankedPaletteRegistry();
    expect(registry.getViews().map((v) => v.id)).toContain("commands");
  });

  it("renders OUR view on the FIRST commit, not the DS's auto-synthesized one", () => {
    const registry = createRankedPaletteRegistry();
    registry.registerCommands([cmd({ id: "a", label: "Alpha" })]);
    mountPalette(registry);
    // `.pa-cmdk` is ours; the DS's commandsView renders `.pl-cmdk-commands` alone. A view
    // registered from an effect would fail here and only pass after a version bump.
    expect(document.querySelector(".pa-cmdk")).not.toBeNull();
    expect(labels()).toEqual(["Alpha"]);
  });

  it("leaves the DS default in place when nothing claims the root id", () => {
    // Pins WHY the mechanism works: view replacement, keyed on the literal "commands" that
    // both mount sites leave at its default. Lose the id and the DS silently takes over.
    const bare = createPaletteRegistry();
    bare.registerCommands([cmd({ id: "a", label: "Alpha" })]);
    mountPalette(bare);
    expect(document.querySelector(".pl-cmdk-commands")).not.toBeNull();
    expect(document.querySelector(".pa-cmdk")).toBeNull();
  });
});

describe("the empty query is a different list", () => {
  const root3 = [
    cmd({ id: "chat:ask", label: "Ask protoAgent", group: "Agents" }),
    cmd({ id: "fleet-room", label: "Fleet Room", group: "Agents" }),
    cmd({ id: "settings", label: "Settings", group: "Commands" }),
  ];
  const surfaces = [cmd({ id: "open:memory", label: "Memory", group: "Go to" })];
  const recency: RecentMap = { "cmd:settings": { n: 3, t: Date.now() } };

  it("leads with recents, under their own header, then the curated root", () => {
    const list = emptyQueryList(root3, [...root3, ...surfaces], recency);
    expect(list[0].id).toBe("settings");
    expect(list[0].group).toBe(RECENT_GROUP);
    // …and is not duplicated further down.
    expect(list.filter((c) => c.id === "settings")).toHaveLength(1);
  });

  it("can surface a SEARCH-ONLY row as a recent, but never as filler", () => {
    const withSurface: RecentMap = { "cmd:open:memory": { n: 1, t: Date.now() } };
    const list = emptyQueryList(root3, [...root3, ...surfaces], withSurface);
    expect(list[0].id).toBe("open:memory");
    // Nothing else from the surface corpus rides along — that flood is what the split exists
    // to prevent (`matchCommand` returns true for "", so registering surfaces at the root
    // would dump every rail surface into the empty palette).
    expect(list.filter((c) => c.id.startsWith("open:"))).toHaveLength(1);
  });

  it("caps the empty list — and ONLY the empty list", () => {
    const many = Array.from({ length: 30 }, (_, i) => cmd({ id: `c${i}`, label: `Command ${i}` }));
    expect(emptyQueryList(many, many, {}, { emptyCap: 9 })).toHaveLength(9);
  });

  it("drops a recent whose command is gone (a plugin disabled) rather than offering a dead row", () => {
    const stale: RecentMap = { "cmd:nav:plugin:gone:view": { n: 9, t: Date.now() } };
    expect(emptyQueryList(root3, root3, stale).map((c) => c.id)).toEqual(root3.map((c) => c.id));
  });

  it("skips a DISABLED command — a recents list that can't run its top row is noise", () => {
    const off = [cmd({ id: "fleet-room", label: "Fleet Room", disabled: true })];
    expect(emptyQueryList(off, off, { "cmd:fleet-room": { n: 5, t: Date.now() } })[0].group)
      .not.toBe(RECENT_GROUP);
  });
});

describe("search: the whole corpus, ranked", () => {
  const registryWithSurfaces = () => {
    const registry = createRankedPaletteRegistry({
      searchOnly: () => [
        cmd({ id: "open:memory", label: "Memory", group: "Go to", keywords: ["open", "go"] }),
        cmd({ id: "open:knowledge", label: "Knowledge", group: "Go to", keywords: ["open", "go"] }),
        cmd({ id: "open:chat", label: "Chat", group: "Go to", keywords: ["open", "go"] }),
      ],
      emptyCap: 4,
    });
    registry.registerCommands([
      cmd({ id: "chat:ask", label: "Ask protoAgent", group: "Agents", keywords: ["chat"] }),
      cmd({ id: "fleet-room", label: "Fleet Room", group: "Agents", keywords: ["ava", "chat"] }),
      cmd({ id: "open", label: "Open...", group: "Commands", keywords: ["surface"] }),
    ]);
    return registry;
  };

  it("finds a rail surface by name — the exact defect (typing 'memory' rendered No matches)", () => {
    mountPalette(registryWithSurfaces());
    expect(labels()).not.toContain("Memory"); // absent from the empty root, by design
    type("memory");
    expect(labels()).toContain("Memory");
  });

  it("ranks a label match above a keyword-only hit (finding 17's two 'Chat's)", () => {
    mountPalette(registryWithSurfaces());
    type("chat");
    // Three rows match: the Chat SURFACE by label, and the transient palette chat + the
    // Fleet Room by keyword. The surface leads; both keyword rows stay listed, one hop away.
    expect(labels()[0]).toBe("Chat");
    expect(labels()).toEqual(expect.arrayContaining(["Ask protoAgent", "Fleet Room"]));
  });

  it("keeps a keyword-only hit reachable — 'ava' finds the Fleet Room (fleet.spec.ts:431)", () => {
    mountPalette(registryWithSurfaces());
    type("ava");
    // The member names ride the Fleet Room command's keywords; a label-first ranking that
    // dropped keyword matches, or a cap on the query path, would red that e2e spec.
    expect(labels()).toContain("Fleet Room");
  });

  it("does not cap the query path — every match is reachable", () => {
    const registry = createRankedPaletteRegistry({ emptyCap: 3 });
    registry.registerCommands(
      Array.from({ length: 12 }, (_, i) => cmd({ id: `x${i}`, label: `Widget ${i}` })),
    );
    mountPalette(registry);
    expect(rows()).toHaveLength(3); // capped while empty
    type("widget");
    expect(rows()).toHaveLength(12); // uncapped once typed
  });
});

describe("inherited from commandsView — silent if lost", () => {
  it("runs a provider's getCommands and renders its rows, stamped with the source chip", async () => {
    vi.useFakeTimers();
    const registry = createRankedPaletteRegistry();
    const getCommands = vi.fn((q: string) => [cmd({ id: `hit:${q}`, label: `Result for ${q}` })]);
    registry.registerProvider({ id: "p", source: { id: "p", label: "boardy" }, getCommands });
    mountPalette(registry);

    type("proj");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200); // past the 120ms debounce
    });
    expect(getCommands).toHaveBeenCalled();
    expect(getCommands.mock.calls[0][0]).toBe("proj");
    expect(labels()).toContain("Result for proj");
    expect(document.querySelector(".pl-cmdk-commands__chip")?.textContent).toBe("boardy");
  });

  it("never debounces or spins on an EMPTY query — the palette opens straight onto recents", () => {
    vi.useFakeTimers();
    const registry = createRankedPaletteRegistry();
    const getCommands = vi.fn(() => []);
    registry.registerProvider({ id: "p", getCommands });
    registry.registerCommands([cmd({ id: "a", label: "Alpha" })]);
    mountPalette(registry);
    // No spinner on open, and no provider round-trip: a later live-search PR must not turn
    // "press the palette open" into 120ms of "Searching...".
    expect(document.querySelector(".pl-cmdk-commands__spinner")).toBeNull();
    expect(getCommands).not.toHaveBeenCalled();
    expect(labels()).toEqual(["Alpha"]);
  });

  it("shows the spinner while a typed query is in flight, then swaps to the results", async () => {
    vi.useFakeTimers();
    const registry = createRankedPaletteRegistry();
    registry.registerProvider({ id: "p", getCommands: () => [cmd({ id: "r", label: "Remote hit" })] });
    mountPalette(registry);
    type("remote");
    expect(document.querySelector(".pl-cmdk-commands__spinner")).not.toBeNull();
    expect(document.querySelector(".pl-cmdk-commands__empty")?.textContent).toBe("Searching…");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(document.querySelector(".pl-cmdk-commands__spinner")).toBeNull();
    expect(labels()).toEqual(["Remote hit"]);
  });

  it("keeps the DS markup contract two e2e specs assert on", () => {
    const registry = createRankedPaletteRegistry();
    registry.registerCommands([cmd({ id: "a", label: "Alpha", group: "Agents", hint: "go to" })]);
    mountPalette(registry);
    // fleet.spec.ts:426 / keybindings.spec.ts:11-18
    expect(document.querySelector(".pl-cmdk__panel .pl-cmdk-commands__input")).not.toBeNull();
    expect(input().getAttribute("role")).toBe("combobox");
    expect(document.querySelector('[role="listbox"]')).not.toBeNull();
    expect(rows()[0].getAttribute("aria-selected")).toBe("true");
    expect(document.querySelector(".pl-cmdk-commands__group")?.textContent).toBe("Agents");
    expect(document.querySelector(".pl-cmdk-commands__hint")?.textContent).toBe("go to");
  });

  it("renders a group header only when the group CHANGES (contiguity, not grouping)", () => {
    const registry = createRankedPaletteRegistry();
    registry.registerCommands([
      cmd({ id: "a", label: "A", group: "Agents" }),
      cmd({ id: "b", label: "B", group: "Agents" }),
      cmd({ id: "c", label: "C", group: "Commands" }),
    ]);
    mountPalette(registry);
    expect([...document.querySelectorAll(".pl-cmdk-commands__group")].map((g) => g.textContent))
      .toEqual(["Agents", "Commands"]);
  });

  it("wraps around on ArrowUp/ArrowDown and runs the selection on Enter", () => {
    const ran: string[] = [];
    const registry = createRankedPaletteRegistry();
    registry.registerCommands(
      ["A", "B", "C"].map((l) => cmd({ id: l, label: l, run: () => ran.push(l) })),
    );
    mountPalette(registry);
    press("ArrowUp"); // wraps to the last row
    expect(selected()).toContain("C");
    press("ArrowDown"); // wraps back to the first
    expect(selected()).toContain("A");
    press("ArrowDown");
    press("Enter");
    expect(ran).toEqual(["B"]);
  });

  it("will not run a disabled row", () => {
    const run = vi.fn();
    const registry = createRankedPaletteRegistry();
    registry.registerCommands([cmd({ id: "a", label: "Fleet Room", disabled: true, run })]);
    mountPalette(registry);
    press("Enter");
    expect(run).not.toHaveBeenCalled();
    expect(rows()[0].className).toContain("pl-cmdk-commands__item--disabled");
  });

  it("re-renders when the registry changes under it", () => {
    const registry = createRankedPaletteRegistry();
    registry.registerCommands([cmd({ id: "a", label: "Alpha" })]);
    mountPalette(registry);
    act(() => {
      registry.registerCommands([cmd({ id: "b", label: "Beta" })]);
    });
    expect(labels()).toEqual(["Alpha", "Beta"]);
  });

  it("says 'No matches' when nothing survives the filter", () => {
    const registry = createRankedPaletteRegistry();
    registry.registerCommands([cmd({ id: "a", label: "Alpha" })]);
    mountPalette(registry);
    type("zzzz");
    expect(document.querySelector(".pl-cmdk-commands__empty")?.textContent).toBe("No matches");
  });
});

describe("selection follows the COMMAND, not the index", () => {
  it("moves the highlight to the new top row when a re-rank keeps the row count", () => {
    const registry = createRankedPaletteRegistry();
    // Both rows match "ee" and both match "queen": the COUNT never changes, only the ORDER.
    // The DS resets selection on `[filtered.length]`, so it would leave the highlight on
    // row 0 of a list whose row 0 is now a different command — and Enter would run it.
    registry.registerCommands([
      cmd({ id: "beekeeper", label: "Beekeeper queen" }),
      cmd({ id: "queen", label: "Queen bee" }),
    ]);
    mountPalette(registry);
    type("bee");
    expect(rows()).toHaveLength(2);
    expect(selected()).toContain("Beekeeper queen"); // label prefix wins
    type("queen");
    expect(rows()).toHaveLength(2); // same count…
    expect(selected()).toContain("Queen bee"); // …new leader, and the highlight followed
  });

  it("keeps the highlight on the command the operator picked while the list is stable", () => {
    const registry = createRankedPaletteRegistry();
    registry.registerCommands(["A", "B", "C"].map((l) => cmd({ id: l, label: l })));
    mountPalette(registry);
    press("ArrowDown");
    expect(selected()).toContain("B");
    act(() => {
      registry.registerProvider({ id: "noop" }); // a version bump that changes nothing
    });
    expect(selected()).toContain("B");
  });
});

describe("frecency is written where every command runs", () => {
  it("records the run before handing off, whatever contributed the command", () => {
    const seen: string[] = [];
    const registry = createRankedPaletteRegistry({ onRun: (c) => seen.push(c.id) });
    registry.registerCommands([cmd({ id: "settings", label: "Settings" })]);
    mountPalette(registry);
    press("Enter");
    expect(seen).toEqual(["settings"]);
  });

  it("defaults to the real store, so no source can forget to feed it", () => {
    const registry = createRankedPaletteRegistry();
    registry.registerCommands([cmd({ id: "settings", label: "Settings" })]);
    mountPalette(registry);
    press("Enter");
    expect(JSON.parse(localStorage.getItem("protoagent.palette.recent")!)["cmd:settings"].n).toBe(1);
  });

  it("orders the empty list by what was actually run", () => {
    const registry = createRankedPaletteRegistry();
    registry.registerCommands([
      cmd({ id: "first", label: "First" }),
      cmd({ id: "second", label: "Second" }),
    ]);
    mountPalette(registry);
    press("ArrowDown");
    press("Enter"); // runs "Second"
    act(() => root.unmount()); // the palette closes…
    root = createRoot(container);
    mountPalette(registry); // …and reopens, re-reading the store
    expect(labels()[0]).toBe("Second");
    expect(document.querySelector(".pl-cmdk-commands__group")?.textContent).toBe(RECENT_GROUP);
  });
});

describe("paletteRootView", () => {
  it("claims the DS's default root id and its default width", () => {
    const view = paletteRootView({ getRegistry: () => createPaletteRegistry() });
    expect(view.id).toBe("commands");
    expect(view.width).toBe(560);
    expect(view.footerHint).toBeTruthy();
  });
});
