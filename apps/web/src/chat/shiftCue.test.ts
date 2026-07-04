import { afterEach, describe, expect, it, vi } from "vitest";

import { ADD_SELECTOR, isIncognitoAddClick, trackShiftHeld } from "./shiftCue";

function keydown(shiftKey: boolean) {
  window.dispatchEvent(new KeyboardEvent("keydown", { shiftKey }));
}
function keyup(shiftKey: boolean) {
  window.dispatchEvent(new KeyboardEvent("keyup", { shiftKey }));
}

describe("trackShiftHeld", () => {
  let cleanup: (() => void) | undefined;
  afterEach(() => {
    cleanup?.();
    cleanup = undefined;
  });

  it("reports Shift held on keydown and released on keyup", () => {
    const onChange = vi.fn();
    cleanup = trackShiftHeld(onChange);
    keydown(true);
    expect(onChange).toHaveBeenLastCalledWith(true);
    keyup(false);
    expect(onChange).toHaveBeenLastCalledWith(false);
  });

  it("clears on window blur (a blur mid-hold must not strand the cue on)", () => {
    const onChange = vi.fn();
    cleanup = trackShiftHeld(onChange);
    keydown(true);
    expect(onChange).toHaveBeenLastCalledWith(true);
    window.dispatchEvent(new Event("blur"));
    expect(onChange).toHaveBeenLastCalledWith(false);
  });

  it("detaches every listener on cleanup", () => {
    const onChange = vi.fn();
    trackShiftHeld(onChange)(); // attach, then immediately clean up
    keydown(true);
    keyup(false);
    window.dispatchEvent(new Event("blur"));
    expect(onChange).not.toHaveBeenCalled();
  });
});

// The + → eye swap is CSS-driven: while Shift is held the tab-strip wrapper carries
// `chat-tabbar-wrap--incognito`, which swaps the DS "+" for the EyeOff glyph (and drops it to
// restore the "+"). This mirrors how ChatSurface toggles the class off the trackShiftHeld
// signal, so it pins the +→eye→+ flip that the issue asks for.
describe("Shift toggles the add-button eye cue", () => {
  it("adds --incognito on Shift keydown (+ → eye) and removes it on keyup (eye → +)", () => {
    const wrap = document.createElement("div");
    wrap.className = "chat-tabbar-wrap";
    const cleanup = trackShiftHeld((held) => {
      wrap.classList.toggle("chat-tabbar-wrap--incognito", held);
    });

    keydown(true);
    expect(wrap.classList.contains("chat-tabbar-wrap--incognito")).toBe(true);

    keyup(false);
    expect(wrap.classList.contains("chat-tabbar-wrap--incognito")).toBe(false);

    cleanup();
  });
});

// Guards the existing Shift+click gesture (#1697): swapping the icon must not change WHEN a new
// incognito chat is created — only Shift + a click on the add "+".
describe("isIncognitoAddClick (the incognito click path is unchanged)", () => {
  function addButton() {
    const btn = document.createElement("button");
    btn.className = "pl-tabbar__add";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    btn.appendChild(svg);
    return { btn, svg };
  }

  it("is true for a Shift+click on (or inside) the add button", () => {
    const { btn, svg } = addButton();
    expect(isIncognitoAddClick(btn, true)).toBe(true);
    expect(isIncognitoAddClick(svg, true)).toBe(true); // closest() climbs from the inner glyph
  });

  it("is false without Shift (a plain click still opens a normal chat)", () => {
    const { btn } = addButton();
    expect(isIncognitoAddClick(btn, false)).toBe(false);
  });

  it("is false for a Shift+click that misses the add button", () => {
    const close = document.createElement("button");
    close.className = "pl-tabbar__close";
    expect(isIncognitoAddClick(close, true)).toBe(false);
    expect(isIncognitoAddClick(null, true)).toBe(false);
  });

  it("matches on the DS add-button selector", () => {
    expect(ADD_SELECTOR).toBe(".pl-tabbar__add");
  });
});
