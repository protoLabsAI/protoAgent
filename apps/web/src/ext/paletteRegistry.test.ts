import { describe, expect, it, vi } from "vitest";

import {
  paletteCommandsVersion,
  registerPaletteCommand,
  registerPaletteSource,
  registeredPaletteCommands,
  subscribePaletteCommands,
} from "./paletteRegistry";
import type { PaletteCommand, PaletteGateContext } from "./paletteRegistry";

const byId = (id: string) => registeredPaletteCommands().find((c) => c.id === id);
const gate: PaletteGateContext = { flagOn: () => true, isHost: true };

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
      shortcut: "⌘⇧K",
      disabled: true,
      disabledReason: "host instance only",
      when: () => true,
      run: () => {},
    });
    const cmd = byId("p3")!;
    expect(cmd.icon).toBe(icon);
    expect(cmd.hint).toBe("to the web");
    expect(cmd.shortcut).toBe("⌘⇧K");
    expect(cmd.disabled).toBe(true);
    expect(cmd.disabledReason).toBe("host instance only");
    expect(cmd.when?.(gate)).toBe(true);
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

    unsub();
    registerPaletteCommand({ id: "p7", label: "Seven", run: () => {} });
    expect(seen).toHaveBeenCalledTimes(2); // unsubscribed
    expect(paletteCommandsVersion()).toBeGreaterThan(v1 + 1); // version still moves
  });

  it("re-reads a dynamic source on every read (never cached)", () => {
    let tabs = ["alpha"];
    const source = vi.fn(
      (): PaletteCommand[] =>
        tabs.map((t) => ({ id: `tab:${t}`, label: t, run: () => {} })),
    );
    const off = registerPaletteSource(source);

    expect(byId("tab:alpha")).toBeDefined();
    expect(byId("tab:beta")).toBeUndefined();

    tabs = ["alpha", "beta"]; // live data changed, with NO re-registration
    expect(byId("tab:beta")).toBeDefined();
    expect(source.mock.calls.length).toBeGreaterThan(1);

    off();
    expect(byId("tab:alpha")).toBeUndefined();
    off(); // idempotent
    expect(byId("tab:alpha")).toBeUndefined();
  });

  it("a throwing source does not blank the palette", () => {
    registerPaletteCommand({ id: "p8", label: "Eight", run: () => {} });
    const off = registerPaletteSource(() => {
      throw new Error("fork bug");
    });
    expect(byId("p8")).toBeDefined();
    off();
  });

  it("does NOT evaluate `when` at registration time", () => {
    const when = vi.fn(() => true);
    const off = registerPaletteCommand({ id: "p9", label: "Gated", when, run: () => {} });
    expect(when).not.toHaveBeenCalled();
    // Reading the registry doesn't evaluate it either — the gate is the ROOT VIEW's, run
    // per render, so a flag that lands after registration can still flip the row on.
    registeredPaletteCommands();
    expect(when).not.toHaveBeenCalled();
    expect(byId("p9")!.when!(gate)).toBe(true);
    expect(when).toHaveBeenCalledTimes(1);
    off();
  });

  it("core deep-links are dogfooded through the same seam", async () => {
    // Importing usePaletteRegistry runs its module-load registrations (the core deep-links).
    await import("../app/usePaletteRegistry");
    const ids = registeredPaletteCommands().map((c) => c.id);
    expect(ids).toContain("settings");
    expect(ids).toContain("plug:market");
  });
});
