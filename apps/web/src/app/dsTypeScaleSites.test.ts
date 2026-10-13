import { describe, expect, it } from "vitest";

// #3688 part 5 — the workflows / goals / watches / agent-identity stylesheets moved every
// hard-coded px font-size onto the DS type scale (var(--pl-font-size-{3xs..xl}), no px
// fallback). This guard asserts on the raw stylesheet text (same source-guard pattern as
// chat-css-tokens.test.ts / app/dsTypeScale.test.ts) so a regression — a re-introduced px
// literal or a token that grows a stray fallback — fails loudly here. It reads only the CSS
// text, never the DS package's token values, so it stands independent of the ^0.10.0 bump
// that dsTypeScale.test.ts pins. Vitest opts src's CSS into processing (vitest.config.ts
// `test.css.include`), which is what lets `?raw` return the real text instead of "".
import agentIdentityCss from "../agent/identity.css?raw";
import goalsCss from "../goals/goals.css?raw";
import watchesCss from "../watches/watches.css?raw";
import workflowsCss from "../workflows/workflows.css?raw";

const FILES: Record<string, string> = {
  "workflows/workflows.css": workflowsCss,
  "goals/goals.css": goalsCss,
  "watches/watches.css": watchesCss,
  "agent/identity.css": agentIdentityCss,
};

// The seven scale steps this card maps sites onto.
const STEP = "(?:3xs|2xs|xs|sm|base|lg|xl)";
// A bare DS type-scale token: `var(--pl-font-size-<step>)` with NO comma-list fallback.
const BARE_TOKEN = new RegExp(`^var\\(--pl-font-size-${STEP}\\)$`);

// Pull a single top-level rule's body by its exact line-start selector (each targeted rule is
// flat, so `[^}]*` is a safe body matcher). Same helper as chat-css-tokens.test.ts.
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
  expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
  return match![0];
}

describe("#3688 p5 — type scale on DS tokens across workflows/goals/watches/identity", () => {
  for (const [name, css] of Object.entries(FILES)) {
    it(`${name} loads as raw text (the guard is not silently blind)`, () => {
      expect(css.length).toBeGreaterThan(0);
    });

    it(`${name} has no hard-coded px font-size`, () => {
      expect(css).not.toMatch(/font-size:\s*[0-9.]+px/);
    });

    it(`${name} every font-size is a bare --pl-font-size-* token, no px fallback`, () => {
      const values = [...css.matchAll(/font-size:\s*([^;]+);/g)].map((m) => m[1].trim());
      expect(values.length, `expected at least one font-size in ${name}`).toBeGreaterThan(0);
      for (const value of values) {
        expect(value, `unexpected font-size \`${value}\` in ${name}`).toMatch(BARE_TOKEN);
      }
    });

    it(`${name} keeps comments free of the glued */ minifier trap`, () => {
      // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a
      // comment early and silently drops downstream rules from the minified bundle.
      expect(css).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
    });
  }
});

// The half-pixel sites that SNAPPED to the nearest step (size is theme-invariant): pin the
// direction of each snap by selector so a later re-round to the wrong step is caught.
describe("#3688 p5 — half-pixel sites snapped to the mapped step", () => {
  const SNAPS: Array<[css: string, file: string, selector: string, token: string, note: string]> = [
    [workflowsCss, "workflows.css", ".run-history-row", "sm", "12.5px → 13px"],
    [workflowsCss, "workflows.css", ".run-history-steps", "2xs", "11.5px → 11px"],
    [workflowsCss, "workflows.css", ".builder-chip", "2xs", "10.5px → 11px"],
    [goalsCss, "goals.css", ".goal-detail-evidence", "2xs", "11.5px → 11px"],
    [goalsCss, "goals.css", ".goal-timeline-reason", "sm", "12.5px → 13px"],
  ];

  for (const [css, file, selector, token, note] of SNAPS) {
    it(`${file} ${selector} (${note}) reads --pl-font-size-${token}`, () => {
      expect(rule(css, selector)).toMatch(
        new RegExp(`font-size:\\s*var\\(--pl-font-size-${token}\\);`),
      );
    });
  }
});
