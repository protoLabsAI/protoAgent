// #2467 — the Keyboard panel's browser-reserved-shortcut hint derives from
// formatCombo, so these pin the platform labels the hint (and every binding row)
// renders: macOS glyphs on Mac, Ctrl+ words elsewhere. IS_MAC is resolved at
// module load from navigator.platform, so each case stubs then re-imports.
import { afterEach, describe, expect, it, vi } from "vitest";

async function formatComboOn(platform: string) {
  vi.resetModules();
  vi.stubGlobal("navigator", { platform, userAgent: platform });
  const { formatCombo } = await import("./combo");
  return formatCombo;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("formatCombo platform labels (the reserved-shortcut hint's source)", () => {
  it("renders Windows-style Ctrl labels off-Mac", async () => {
    const formatCombo = await formatComboOn("Win32");
    expect(formatCombo("mod+t")).toBe("Ctrl+T");
    expect(`${formatCombo("mod+1")}–9`).toBe("Ctrl+1–9");
    expect(formatCombo("ctrl+tab")).toBe("Ctrl+Tab");
  });

  it("renders macOS glyphs on Mac", async () => {
    const formatCombo = await formatComboOn("MacIntel");
    expect(formatCombo("mod+t")).toBe("⌘T");
    expect(`${formatCombo("mod+1")}–9`).toBe("⌘1–9");
    expect(formatCombo("ctrl+tab")).toBe("⌃Tab");
  });

  it("renders Linux like Windows (Ctrl words, plus-joined)", async () => {
    const formatCombo = await formatComboOn("Linux x86_64");
    expect(formatCombo("mod+t")).toBe("Ctrl+T");
    expect(formatCombo("ctrl+tab")).toBe("Ctrl+Tab");
  });
});
