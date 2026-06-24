import { describe, expect, it } from "vitest";

import { registerPaletteCommand, registeredPaletteCommands } from "./paletteRegistry";

describe("palette-command registry (ADR 0061)", () => {
  it("registers, first-wins, and ignores invalid", () => {
    registerPaletteCommand({ id: "p1", label: "One", run: () => {} });
    registerPaletteCommand({ id: "p1", label: "Two", run: () => {} });
    expect(registeredPaletteCommands().find((c) => c.id === "p1")?.label).toBe("One");

    registerPaletteCommand({ id: "", label: "x", run: () => {} });
    // @ts-expect-error — missing run
    registerPaletteCommand({ id: "norun", label: "x" });
    expect(registeredPaletteCommands().some((c) => c.id === "")).toBe(false);
    expect(registeredPaletteCommands().some((c) => c.id === "norun")).toBe(false);
  });

  it("run gets a close() context", () => {
    let closed = false;
    registerPaletteCommand({ id: "p2", label: "Two", run: (ctx) => ctx.close() });
    registeredPaletteCommands()
      .find((c) => c.id === "p2")!
      .run({ close: () => (closed = true) });
    expect(closed).toBe(true);
  });

  it("core deep-links are dogfooded through the same seam", async () => {
    // Importing usePaletteRegistry runs its module-load registrations (the core deep-links).
    await import("../app/usePaletteRegistry");
    const ids = registeredPaletteCommands().map((c) => c.id);
    expect(ids).toContain("settings");
    expect(ids).toContain("plug:market");
  });
});
