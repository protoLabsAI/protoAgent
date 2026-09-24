import { afterEach, describe, expect, it, vi } from "vitest";

import { EDITOR_PREF_KEY, getEditorPref, setEditorPref } from "./editorPref";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("editor preference", () => {
  it("defaults to Zed and round-trips through localStorage", () => {
    localStorage.clear();
    expect(getEditorPref()).toBe("zed");
    setEditorPref("cursor");
    expect(localStorage.getItem(EDITOR_PREF_KEY)).toBe("cursor");
    expect(getEditorPref()).toBe("cursor");
  });

  it("ignores a garbage stored value", () => {
    localStorage.setItem(EDITOR_PREF_KEY, "emacs");
    setEditorPref("zed"); // resets the in-memory fallback to the default
    localStorage.setItem(EDITOR_PREF_KEY, "emacs");
    expect(getEditorPref()).toBe("zed");
  });

  it("survives storage that throws, keeping the choice in memory", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => setEditorPref("off")).not.toThrow();
    expect(getEditorPref()).toBe("off");
  });
});
