// A plugin names its glyph by STRING, in two manifest places (`views[].icon` and, since
// #3294, `commands[].icon`) — and both strings reach `pluginViewIcon` verbatim: the parser
// keeps any non-empty name, because the console is what knows the icon set. That makes the
// lookup a trust boundary, and it sits inside App's render and the palette-registry effect,
// so a throw here is not a missing glyph — it is the ROOT error boundary replacing the whole
// console with the crash card, still crashed on reload because the manifest is still
// installed.
import { createElement as h } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { pluginViewIcon } from "./pluginIcon";

// React only honors `act()` when the environment opts in.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
});
afterEach(() => host.remove());

function render(name?: string): string {
  const root = createRoot(host);
  act(() => root.render(h("div", null, pluginViewIcon(name))));
  const html = host.innerHTML;
  act(() => root.unmount());
  return html;
}

describe("pluginViewIcon", () => {
  it("renders a curated glyph by name", () => {
    expect(render("Sparkles")).toContain("<svg");
  });

  it("falls back to the plugin mark for a name it does not know", () => {
    // The lazy full-lucide path renders its Suspense fallback synchronously here; either
    // way an unknown name must be an SVG, not a throw.
    expect(render("NopeIcon")).toContain("<svg");
    expect(render()).toContain("<svg");
  });

  it("does not resolve an Object.prototype key as a component", () => {
    // The curated table is an object LITERAL, so a bare `TABLE[name]` answers `Object` for
    // "constructor" and `hasOwnProperty` for "hasOwnProperty" — React then CALLS them.
    // Before the own-property guard these threw "Objects are not valid as a React child" and
    // "Cannot convert undefined or null to object" respectively, and "toString" rendered the
    // literal text "[object Undefined]" into the rail.
    for (const name of ["constructor", "hasOwnProperty", "isPrototypeOf", "toString", "valueOf", "__proto__"]) {
      const html = render(name);
      expect(html, name).toContain("<svg");
      expect(html, name).not.toContain("[object");
    }
  });
});
