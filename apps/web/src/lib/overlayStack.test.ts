import { afterEach, describe, expect, it } from "vitest";

import { isTopmostOverlay } from "./overlayStack";

// Stacked DS Dialogs: only the top-most layer may take an Escape (the set-up dialog over
// Settings, the folder picker over the set-up dialog).
function overlay(dialogClass: string): HTMLElement {
  const o = document.createElement("div");
  o.className = "pl-overlay";
  o.innerHTML = `<div class="pl-dialog ${dialogClass}"></div>`;
  document.body.appendChild(o);
  return o;
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("isTopmostOverlay", () => {
  it("a lone dialog is top-most", () => {
    overlay("settings-overlay");
    expect(isTopmostOverlay(".settings-overlay")).toBe(true);
  });

  it("a dialog with another stacked above it is not; the upper one is", () => {
    overlay("settings-overlay");
    const setup = overlay("archetype-setup-dialog");
    expect(isTopmostOverlay(".settings-overlay")).toBe(false);
    expect(isTopmostOverlay(".archetype-setup-dialog")).toBe(true);
    overlay("path-browser"); // the folder picker opens on top
    expect(isTopmostOverlay(".archetype-setup-dialog")).toBe(false);
    setup.remove();
    expect(isTopmostOverlay(".path-browser")).toBe(true);
  });

  it("an absent dialog doesn't block anything", () => {
    expect(isTopmostOverlay(".nope")).toBe(true);
  });
});
