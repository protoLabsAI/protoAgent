import { afterEach, describe, expect, it, vi } from "vitest";

import { PREVIEW_ARCHETYPES_KEY, getShowPreviewArchetypes, setShowPreviewArchetypes } from "./previewArchetypesPref";

// "Show preview archetypes" — per console, OFF unless the operator turned it on, and the
// console must keep working when storage throws (private mode / blocked site data).

afterEach(() => {
  vi.restoreAllMocks();
  try {
    localStorage.removeItem(PREVIEW_ARCHETYPES_KEY);
  } catch {
    /* ignore */
  }
});

describe("previewArchetypesPref", () => {
  it("is off by default", () => {
    setShowPreviewArchetypes(false); // reset the module's in-memory copy
    localStorage.removeItem(PREVIEW_ARCHETYPES_KEY);
    expect(getShowPreviewArchetypes()).toBe(false);
  });

  it("persists the opt-in under its own key", () => {
    setShowPreviewArchetypes(true);
    expect(localStorage.getItem(PREVIEW_ARCHETYPES_KEY)).toBe("1");
    expect(getShowPreviewArchetypes()).toBe(true);
    setShowPreviewArchetypes(false);
    expect(localStorage.getItem(PREVIEW_ARCHETYPES_KEY)).toBe("0");
    expect(getShowPreviewArchetypes()).toBe(false);
  });

  it("survives storage that throws — the choice holds in memory for the session", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(() => setShowPreviewArchetypes(true)).not.toThrow();
    expect(getShowPreviewArchetypes()).toBe(true);
    setShowPreviewArchetypes(false);
    expect(getShowPreviewArchetypes()).toBe(false);
  });
});
