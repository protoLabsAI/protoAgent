import { afterEach, describe, expect, it, vi } from "vitest";

import { downloadTextFile, safeFilename } from "./download";

describe("safeFilename", () => {
  it("keeps a readable stem", () => {
    expect(safeFilename("My Merck chat")).toBe("My Merck chat");
  });
  it("strips path-hostile characters", () => {
    expect(safeFilename("re: prod/incident?")).toBe("re- prod-incident");
  });
  it("collapses whitespace and trims edge dots/dashes", () => {
    expect(safeFilename("  ..a   b..  ")).toBe("a b");
  });
  it("falls back when nothing survives", () => {
    expect(safeFilename("///")).toBe("chat");
    expect(safeFilename("")).toBe("chat");
  });
});

describe("downloadTextFile — reports whether the click dispatched (#2197)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("returns true once the anchor click has dispatched", () => {
    // Stub the Blob-URL pair to observe the revoke; fake timers keep the deferred
    // revoke inside the stub's lifetime.
    vi.useFakeTimers();
    const createObjectURL = vi.fn(() => "blob:export");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click");

    expect(downloadTextFile("chat.md", "# hi")).toBe(true);

    expect(click).toHaveBeenCalledTimes(1);
    vi.runAllTimers();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:export");
    click.mockRestore();
  });

  it("returns false — still without throwing — when the download is blocked", () => {
    // A blocked surface (sandboxed webview / policy) surfaces as a throw inside the try
    // block — modeled at the Blob-URL mint. The no-throw contract swallows it; the
    // caller learns via the return value.
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => {
        throw new Error("blocked");
      }),
      revokeObjectURL: vi.fn(),
    });
    expect(() => {
      expect(downloadTextFile("chat.md", "# hi")).toBe(false);
    }).not.toThrow();
  });
});
