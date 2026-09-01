import { afterEach, describe, expect, it, vi } from "vitest";

import {
  hasPaletteSources,
  paletteCommandsVersion,
  registerPaletteCommand,
  registerPaletteSource,
  registeredPaletteCommands,
  subscribePaletteCommands,
  visiblePaletteCommands,
} from "./paletteRegistry";
import type { PaletteCommand, PaletteCommandSource } from "./paletteRegistry";

const byId = (id: string) => registeredPaletteCommands().find((c) => c.id === id);
// The registry is a module singleton and a source contributes to EVERY read, so one left
// behind by a failed assertion poisons every later test (a throwing one most of all). Statics
// are keyed by unique ids and harmless; sources get torn down after each test.
const sourceOffs: (() => void)[] = [];
const source = (fn: PaletteCommandSource) => {
  const off = registerPaletteSource(fn);
  sourceOffs.push(off);
  return off;
};
afterEach(() => sourceOffs.splice(0).forEach((off) => off()));
const visibleIds = (
  flagOn: (id: string) => boolean,
  onHost?: boolean,
  from?: "all" | "static" | "dynamic",
) => visiblePaletteCommands(flagOn, onHost, from).map((c) => c.id);
const allOn = () => true;

describe("palette-command registry (ADR 0061)", () => {
  it("registers, LAST-wins, and ignores invalid", () => {
    registerPaletteCommand({ id: "p1", label: "One", run: () => {} });
    registerPaletteCommand({ id: "p1", label: "Two", run: () => {} });
    // Last-wins (HMR-safe: a re-evaluated module replaces its own entry)…
    expect(byId("p1")?.label).toBe("Two");
    // …in the ORIGINAL display position — a re-registration must not reorder the palette.
    registerPaletteCommand({ id: "p1-after", label: "After", run: () => {} });
    registerPaletteCommand({ id: "p1", label: "Three", run: () => {} });
    const ids = registeredPaletteCommands().map((c) => c.id);
    expect(ids.indexOf("p1")).toBeLessThan(ids.indexOf("p1-after"));

    // A padded id is stored TRIMMED, so the dedup key and the id the palette renders agree
    // (` p1 ` replaces `p1` rather than shadowing it with a second row).
    registerPaletteCommand({ id: " p1 ", label: "Trimmed", run: () => {} });
    expect(registeredPaletteCommands().filter((c) => c.id.trim() === "p1")).toHaveLength(1);
    expect(byId("p1")?.label).toBe("Trimmed");

    registerPaletteCommand({ id: "", label: "x", run: () => {} });
    // @ts-expect-error — missing run
    registerPaletteCommand({ id: "norun", label: "x" });
    expect(registeredPaletteCommands().some((c) => c.id === "")).toBe(false);
    expect(byId("norun")).toBeUndefined();
  });

  it("run gets a close() context", () => {
    let closed = false;
    registerPaletteCommand({ id: "p2", label: "Two", run: (ctx) => ctx.close() });
    byId("p2")!.run({ close: () => (closed = true) });
    expect(closed).toBe(true);
  });

  it("carries the presentation fields through registration", () => {
    const icon = "★";
    const off = registerPaletteCommand({
      id: "p3",
      label: "Publish",
      group: "Commands",
      keywords: ["ship"],
      icon,
      hint: "to the web",
      keybinding: "publish.run",
      disabled: true,
      run: () => {},
    });
    const cmd = byId("p3")!;
    expect(cmd.icon).toBe(icon);
    expect(cmd.hint).toBe("to the web");
    expect(cmd.keybinding).toBe("publish.run");
    expect(cmd.disabled).toBe(true);
    // A disabled row is still LISTED — that's the difference between `disabled` and a gate.
    expect(visibleIds(allOn)).toContain("p3");
    off();
  });

  it("unregisters exactly one command, idempotently", () => {
    const off = registerPaletteCommand({ id: "p4", label: "Gone", run: () => {} });
    registerPaletteCommand({ id: "p4-keep", label: "Kept", run: () => {} });
    const before = registeredPaletteCommands().length;
    off();
    expect(byId("p4")).toBeUndefined();
    expect(byId("p4-keep")).toBeDefined();
    expect(registeredPaletteCommands()).toHaveLength(before - 1);
    off(); // idempotent — a second call removes nothing
    expect(registeredPaletteCommands()).toHaveLength(before - 1);
  });

  it("a stale unregister cannot evict a newer registration of the same id", () => {
    const stale = registerPaletteCommand({ id: "p5", label: "Old", run: () => {} });
    registerPaletteCommand({ id: "p5", label: "New", run: () => {} });
    stale();
    expect(byId("p5")?.label).toBe("New");
  });

  it("bumps the version and notifies subscribers on register + unregister", () => {
    const seen = vi.fn();
    const unsub = subscribePaletteCommands(seen);
    const v0 = paletteCommandsVersion();

    const off = registerPaletteCommand({ id: "p6", label: "Six", run: () => {} });
    expect(seen).toHaveBeenCalledTimes(1);
    const v1 = paletteCommandsVersion();
    expect(v1).toBeGreaterThan(v0);

    off();
    expect(seen).toHaveBeenCalledTimes(2);
    expect(paletteCommandsVersion()).toBeGreaterThan(v1);

    // A source moves the version too — adding one changes what the next read returns, and
    // the root view only re-reads when the snapshot changes.
    const offSource = source(() => []);
    expect(seen).toHaveBeenCalledTimes(3);
    offSource();
    expect(seen).toHaveBeenCalledTimes(4);

    unsub();
    const vLast = paletteCommandsVersion();
    registerPaletteCommand({ id: "p7", label: "Seven", run: () => {} });
    expect(seen).toHaveBeenCalledTimes(4); // unsubscribed
    expect(paletteCommandsVersion()).toBeGreaterThan(vLast); // version still moves
  });

  it("re-reads a dynamic source on every read (never cached)", () => {
    let tabs = ["alpha"];
    const makeRows = vi.fn((): PaletteCommand[] =>
      tabs.map((t) => ({ id: `tab:${t}`, label: t, run: () => {} })),
    );
    const off = source(makeRows);

    expect(byId("tab:alpha")).toBeDefined();
    expect(byId("tab:beta")).toBeUndefined();

    tabs = ["alpha", "beta"]; // live data changed, with NO re-registration
    expect(byId("tab:beta")).toBeDefined();
    expect(makeRows.mock.calls.length).toBeGreaterThan(1);

    off();
    expect(byId("tab:alpha")).toBeUndefined();
    off(); // idempotent
    expect(byId("tab:alpha")).toBeUndefined();
  });

  it("a throwing source is skipped without blanking the palette or the sources after it", () => {
    registerPaletteCommand({ id: "p8", label: "Eight", run: () => {} });
    const offBad = source(() => {
      throw new Error("fork bug");
    });
    const offGood = source(() => [
      { id: "p8:src", label: "From a source", run: () => {} },
    ]);
    expect(byId("p8")).toBeDefined(); // statics survive
    expect(byId("p8:src")).toBeDefined(); // …and so does the source registered AFTER the thrower
    offBad();
    offGood();
  });

  it("a source that returns something other than an array is skipped, not thrown out of", () => {
    registerPaletteCommand({ id: "p8b", label: "Static", run: () => {} });
    // Every one of these TYPES as `() => PaletteCommand[]` at the fork's call site (the cast is
    // what a fork's own mistake looks like from here) and every one used to throw
    // `rows is not iterable` STRAIGHT OUT of this read — through the host's effect and onto the
    // console's root ErrorBoundary, replacing the whole app with the crash card.
    const offAsync = source((async () => []) as never); // `async` source → a Promise, not an array
    const offObject = source((() => ({ a: { id: "obj", label: "Obj", run: () => {} } })) as never);
    const offFalse = source((() => false) as never); // "nothing to show", the falsy way
    const offNull = source((() => null) as never);
    const offGood = source(() => [{ id: "p8b:src", label: "From a source", run: () => {} }]);

    expect(() => registeredPaletteCommands()).not.toThrow();
    expect(byId("p8b")).toBeDefined(); // statics survive
    expect(byId("p8b:src")).toBeDefined(); // …and the source registered AFTER the broken ones
    expect(byId("obj")).toBeUndefined(); // an object's VALUES are not harvested as rows
    // Gated reads sit on the same call, so they must not throw either.
    expect(() => visiblePaletteCommands(allOn, false)).not.toThrow();

    offGood();
    offNull();
    offFalse();
    offObject();
    offAsync();
  });

  it("keeps the rows a source produced before it threw part way through", () => {
    const off = source(() => {
      const rows = [{ id: "half:one", label: "One", run: () => {} }];
      // A getter that throws on the SECOND row — a generated list hitting a bad record.
      Object.defineProperty(rows, "1", {
        get() {
          throw new Error("bad record");
        },
        enumerable: true,
      });
      rows.length = 2;
      return rows;
    });
    expect(() => registeredPaletteCommands()).not.toThrow();
    expect(byId("half:one")).toBeDefined(); // half a list beats none
    off();
  });

  it("reads the two halves apart, because the host feeds them to the palette differently", () => {
    const off = registerPaletteCommand({ id: "half:static", label: "Static", run: () => {} });
    const calls = vi.fn();
    const offSource = source(() => {
      calls();
      return [
        { id: "half:dynamic", label: "Dynamic", run: () => {} },
        { id: "half:static", label: "Source shadow", run: () => {} }, // loses to the static
      ];
    });

    const staticIds = registeredPaletteCommands("static").map((c) => c.id);
    expect(staticIds).toContain("half:static");
    expect(staticIds).not.toContain("half:dynamic");
    expect(calls).not.toHaveBeenCalled(); // a statics-only read never CALLS a source

    const dynamicIds = registeredPaletteCommands("dynamic").map((c) => c.id);
    expect(dynamicIds).toEqual(["half:dynamic"]);
    // Static-beats-source holds in the narrowed read too — otherwise the shadow row would
    // reappear through the provider path the host serves "dynamic" on.
    expect(dynamicIds).not.toContain("half:static");
    // Gating applies to a narrowed read exactly as it does to the whole one.
    expect(visibleIds(allOn, true, "dynamic")).toEqual(["half:dynamic"]);
    expect(registeredPaletteCommands().map((c) => c.id)).toEqual(
      expect.arrayContaining(["half:static", "half:dynamic"]),
    );

    offSource();
    off();
  });

  it("reports whether any source is registered, so the host wires the provider only then", () => {
    // The registry-level fact, and the reason the host asks before wiring the DS provider:
    // it shows a "Searching…" spinner (and debounces 120ms) whenever ANY provider declares
    // `getCommands`. (The host's own conditional is pinned separately, in
    // paletteSourceProvider.test.ts — this boolean being right is not the same fact as the
    // effect acting on it.)
    expect(hasPaletteSources()).toBe(false); // core registers none
    const off = source(() => []);
    expect(hasPaletteSources()).toBe(true); // …even though the source yields no rows
    off();
    expect(hasPaletteSources()).toBe(false);
  });

  it("resolves id collisions: a static beats a source, and the first source beats the rest", () => {
    const off = registerPaletteCommand({ id: "dup", label: "Static", run: () => {} });
    const offFirst = source(() => [
      { id: "dup", label: "First source", run: () => {} },
      { id: "dup:only-first", label: "Only first", run: () => {} },
    ]);
    const offSecond = source(() => [
      { id: "dup", label: "Second source", run: () => {} },
      { id: "dup:only-first", label: "Also second", run: () => {} },
    ]);
    expect(byId("dup")?.label).toBe("Static");
    expect(byId("dup:only-first")?.label).toBe("Only first");
    // Source rows land AFTER the statics, in source-registration order.
    const ids = registeredPaletteCommands().map((c) => c.id);
    expect(ids.indexOf("dup")).toBeLessThan(ids.indexOf("dup:only-first"));
    // An invalid row from a source is dropped, not rendered as a blank line.
    const offJunk = source(() => [{ id: "  ", label: "junk", run: () => {} }]);
    expect(registeredPaletteCommands().some((c) => !c.id.trim())).toBe(false);
    offJunk();
    offSecond();
    offFirst();
    off();
  });

  it("gates a flagged command at READ time, so a flag that lands later reveals it", () => {
    const flags = new Set<string>();
    const flagOn = (id: string) => flags.has(id); // fail-closed, like useFlagPredicate in flight
    const off = registerPaletteCommand({ id: "p9", label: "Gated", flag: "beta", run: () => {} });

    // Hidden while the flag is off — but still REGISTERED. That distinction is the whole
    // design: gating at registration (when /api/flags has not answered) would hide it forever.
    expect(visibleIds(flagOn)).not.toContain("p9");
    expect(byId("p9")).toBeDefined();

    flags.add("beta"); // /api/flags answered — no re-registration happens
    expect(visibleIds(flagOn)).toContain("p9");
    off();
  });

  it("gates hostOnly commands on the host axis, sources included", () => {
    const off = registerPaletteCommand({ id: "p10", label: "Host thing", hostOnly: true, run: () => {} });
    const offSource = source(() => [
      { id: "p10:src", label: "Host row", hostOnly: true, run: () => {} },
      { id: "p10:src-flag", label: "Flagged row", flag: "beta", run: () => {} },
    ]);

    expect(visibleIds(allOn, true)).toEqual(expect.arrayContaining(["p10", "p10:src"]));
    const offHost = visibleIds(allOn, false);
    expect(offHost).not.toContain("p10");
    expect(offHost).not.toContain("p10:src"); // a source's rows gate the same way statics do
    expect(visibleIds(() => false, false)).not.toContain("p10:src-flag"); // …flags too
    expect(visibleIds(allOn, false)).toContain("p10:src-flag");

    offSource();
    off();
  });

  it(
    "core deep-links are dogfooded through the same seam",
    async () => {
      // Importing usePaletteRegistry runs its module-load registrations (the core deep-links).
      // Kept LAST and dynamic: the import registers into this module's global state, so every
      // test above sees the registry without core's rows in it. The generous timeout is for
      // the transform, not the assertion — the adapter pulls in the DS palette, the UI store,
      // react-query, the flags query, the keybinding store and (since #3292) the chat rows'
      // chat-store/seam modules, and under a full-suite run that cold import blows past
      // vitest's 5s default. It has measured ~30s on a loaded machine, so the cap is well
      // clear of it: this test failing on time says the import graph grew, not that the
      // dogfooding broke.
      await import("../app/usePaletteRegistry");
      const ids = registeredPaletteCommands().map((c) => c.id);
      expect(ids).toContain("settings");
      expect(ids).toContain("plug:market");
      // …and importing the adapter still registers NO source. #3292's chat rows go through
      // the static path (the DS keeps a provider's previous results listed and runnable for
      // the 120ms it debounces the new query — not a thing to do with rows that RUN), so the
      // default console never pays the provider's spinner.
      expect(hasPaletteSources()).toBe(false);
    },
    90_000,
  );
});
