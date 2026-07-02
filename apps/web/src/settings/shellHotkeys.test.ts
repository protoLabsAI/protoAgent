import { describe, expect, it } from "vitest";

import { eventToShellChord, formatShellChord } from "./shellHotkeys";

function ev(init: Partial<KeyboardEvent> & { key: string }): KeyboardEvent {
  return new KeyboardEvent("keydown", init);
}

describe("eventToShellChord", () => {
  it("maps modifiers + key into the global-hotkey grammar", () => {
    expect(eventToShellChord(ev({ key: "p", metaKey: true, shiftKey: true }))).toBe("super+shift+p");
    expect(eventToShellChord(ev({ key: " ", ctrlKey: true, altKey: true }))).toBe("ctrl+alt+space");
    expect(eventToShellChord(ev({ key: "F5", altKey: true }))).toBe("alt+f5");
  });
  it("rejects bare keys, bare modifiers, and unregisterable keys", () => {
    expect(eventToShellChord(ev({ key: "p" }))).toBe(null); // no modifier → would eat typing
    expect(eventToShellChord(ev({ key: "Shift", shiftKey: true }))).toBe(null);
    expect(eventToShellChord(ev({ key: "ArrowUp", ctrlKey: true }))).toBe(null);
  });
});

describe("formatShellChord", () => {
  it("renders mac glyphs and win/linux words", () => {
    expect(formatShellChord("super+shift+p", "MacIntel")).toBe("⌘⇧P");
    expect(formatShellChord("ctrl+alt+space", "Win32")).toBe("Ctrl+Alt+Space");
  });
});
