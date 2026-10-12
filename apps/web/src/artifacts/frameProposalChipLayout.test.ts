// #4076: the send confirm chip's "Always allow" checkbox sat alone mid-row while its label wrapped
// onto two lines at the far right. theme.css's global `input { width: 100% }` stretched the
// checkbox across the trust row, centring the box and squeezing the text. jsdom does no layout,
// so this pins the CSS contract instead (the browser check lives in the PR's screenshots): for
// BOTH chip hosts (the inline artifact frame and the frame-component host), the trust control is
// one unwrapped inline-flex unit that wraps as a whole, and its checkbox opts out of the global
// full-width input rule.
import { describe, expect, it } from "vitest";

import css from "../chat/chat-component.css?raw";

function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  if (!match) throw new Error(`no rule for ${selector}`);
  return match[1];
}

describe("frame proposal chip: the trust checkbox stays with its label (#4076)", () => {
  for (const prefix of ["artifact-ref-inline", "frame-component-host"]) {
    it(`${prefix}: the trust control is one inline-flex unit that never wraps inside`, () => {
      const trust = rule(`.${prefix}__bridge-trust`);
      expect(trust).toMatch(/display:\s*inline-flex/);
      expect(trust).toMatch(/gap:/);
      expect(trust).toMatch(/white-space:\s*nowrap/);
      // Sized to its content (not a forced 100% row), so it wraps below the buttons as a whole.
      expect(trust).toMatch(/flex:\s*0 0 auto/);
    });

    it(`${prefix}: the checkbox opts out of the global input { width: 100% }`, () => {
      const box = rule(`.${prefix}__bridge-trust input[type="checkbox"]`);
      expect(box).toMatch(/width:\s*auto/);
      expect(box).toMatch(/flex:\s*none/);
    });
  }
});
