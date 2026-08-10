// #2466 — one Escape must close only the topmost layer: with a nested radix
// menu/listbox/popover open, the Settings dialog stays; with none (or only a
// tooltip), Escape closes Settings as before.
import { describe, expect, it } from "vitest";

import { escapeCloseAllowed } from "./SettingsOverlay";

function popper(inner: string): HTMLElement {
  const wrap = document.createElement("div");
  wrap.setAttribute("data-radix-popper-content-wrapper", "");
  wrap.innerHTML = inner;
  document.body.appendChild(wrap);
  return wrap;
}

describe("escapeCloseAllowed", () => {
  it("blocks the dialog close while a dropdown menu is open", () => {
    const el = popper('<div role="menu" class="pl-menu">…</div>');
    expect(escapeCloseAllowed()).toBe(false);
    el.remove();
    expect(escapeCloseAllowed()).toBe(true);
  });

  it("blocks for listboxes and popovers too", () => {
    const lb = popper('<div role="listbox">…</div>');
    expect(escapeCloseAllowed()).toBe(false);
    lb.remove();
    const po = popper('<div role="dialog">…</div>');
    expect(escapeCloseAllowed()).toBe(false);
    po.remove();
  });

  it("a hovered tooltip must NOT hold the dialog open", () => {
    const tip = popper('<div role="tooltip">hint</div>');
    expect(escapeCloseAllowed()).toBe(true);
    tip.remove();
  });

  it("no layers → close proceeds", () => {
    expect(escapeCloseAllowed()).toBe(true);
  });
});
