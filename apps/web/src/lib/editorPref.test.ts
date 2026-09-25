import { afterEach, describe, expect, it, vi } from "vitest";

import {
  EDITOR_PREF_KEY,
  getEditorPref,
  getOpenFilesIn,
  OPEN_FILES_IN_KEY,
  setEditorPref,
  setExternalEditor,
  setOpenFilesChoice,
  setOpenFilesIn,
} from "./editorPref";

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

describe("open files in (ADR 0112)", () => {
  it("defaults to the in-app pane and keeps the external editor separately", () => {
    localStorage.clear();
    setEditorPref("zed");
    localStorage.removeItem(EDITOR_PREF_KEY);
    localStorage.removeItem(OPEN_FILES_IN_KEY);
    expect(getOpenFilesIn()).toBe("protoagent");
    setOpenFilesChoice("cursor");
    expect(getOpenFilesIn()).toBe("editor");
    expect(getEditorPref()).toBe("cursor");
    setOpenFilesChoice("protoagent");
    expect(getOpenFilesIn()).toBe("protoagent");
    expect(getEditorPref()).toBe("cursor"); // still what ⌘-click / ↗ use
  });

  it("a default user picking External editor 'None' keeps the pane links on", async () => {
    localStorage.clear();
    // A fresh page with nothing stored: no in-memory choice either.
    vi.resetModules();
    const fresh = await import("./editorPref");
    expect(fresh.getOpenFilesIn()).toBe("protoagent");
    fresh.setExternalEditor("off");
    expect(fresh.getEditorPref()).toBe("off");
    expect(fresh.getOpenFilesIn()).toBe("protoagent");
    // …and still after a reload (a new module instance reading only storage).
    vi.resetModules();
    const reloaded = await import("./editorPref");
    expect(reloaded.getOpenFilesIn()).toBe("protoagent");
    setExternalEditor("zed"); // keep the imported name in use
  });

  it("an operator who had turned links OFF keeps them off under the new default", async () => {
    localStorage.clear();
    localStorage.setItem(EDITOR_PREF_KEY, "off");
    // A fresh page: a new module instance, so no in-memory choice — only the stored "off".
    vi.resetModules();
    const fresh = await import("./editorPref");
    expect(fresh.getOpenFilesIn()).toBe("editor");
    expect(fresh.getEditorPref()).toBe("off");
    // …while a stored "zed" (or nothing) lands on the pane.
    localStorage.setItem(EDITOR_PREF_KEY, "zed");
    expect(fresh.getOpenFilesIn()).toBe("protoagent");
    setOpenFilesIn("protoagent"); // keep the imported names in use
  });
});
