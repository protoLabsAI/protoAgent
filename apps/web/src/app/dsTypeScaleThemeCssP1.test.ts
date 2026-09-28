import { describe, expect, it } from "vitest";

// #3688 part 1 — app/theme.css moved every hard-coded px font-size onto the DS type scale
// (var(--pl-font-size-{3xs..xl}), no px fallback, since DS tokens are always loaded). This
// guard asserts on the raw stylesheet text (same source-guard pattern as
// app/dsTypeScaleSites.test.ts) so a regression — a re-introduced px literal or a token that
// grows a stray fallback — fails loudly here. It reads only the CSS text, never the DS
// package's token values, so it stands independent of the DS bump that dsTypeScale.test.ts
// pins. Vitest opts src's CSS into processing (vitest.config.ts `test.css.include`), which is
// what lets `?raw` return the real text instead of "".
import themeCss from "./theme.css?raw";

// The seven scale steps this card maps sites onto.
const STEP = "(?:3xs|2xs|xs|sm|base|lg|xl)";
// A bare DS type-scale token: `var(--pl-font-size-<step>)` with NO comma-list fallback.
const BARE_TOKEN = new RegExp(`^var\\(--pl-font-size-${STEP}\\)$`);

// Pull a single top-level rule's body by its exact line-start selector (each targeted rule is
// flat, so `[^}]*` is a safe body matcher). Same helper as dsTypeScaleSites.test.ts.
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
  expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
  return match![0];
}

describe("#3688 p1 — type scale on DS tokens in app/theme.css", () => {
  it("theme.css loads as raw text (the guard is not silently blind)", () => {
    expect(themeCss.length).toBeGreaterThan(0);
  });

  it("has no hard-coded px font-size", () => {
    expect(themeCss).not.toMatch(/font-size:\s*[0-9.]+px/);
  });

  it("every --pl-font-size-* reference is bare, no px fallback", () => {
    // Pre-existing rem/em/inherit font-sizes are not px sites (the #3688 grep is px-only), so
    // they are out of scope and left untouched — assert only that every DS type-scale
    // reference this card introduced is a bare token with no comma-list fallback.
    const tokenValues = [...themeCss.matchAll(/font-size:\s*([^;]+);/g)]
      .map((m) => m[1].trim())
      .filter((v) => v.includes("--pl-font-size-"));
    expect(tokenValues.length, "expected at least one DS type-scale font-size").toBeGreaterThan(0);
    for (const value of tokenValues) {
      expect(value, `unexpected font-size \`${value}\` in theme.css`).toMatch(BARE_TOKEN);
    }
  });

  it("keeps comments free of the glued */ minifier trap", () => {
    // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a
    // comment early and silently drops downstream rules from the minified bundle.
    expect(themeCss).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
  });
});

// The half-pixel sites that SNAPPED to the nearest step (size is theme-invariant): pin the
// direction of each snap by selector so a later re-round to the wrong step is caught.
describe("#3688 p1 — half-pixel sites snapped to the mapped step", () => {
  const SNAPS: Array<[selector: string, token: string, note: string]> = [
    [".archetype-preview-desc", "sm", "12.5px → 13px"],
    [".archetype-preview-soul", "2xs", "11.5px → 11px"],
    [".playbook-title strong", "base", "13.5px → 14px"],
    [".playbook-desc", "sm", "12.5px → 13px"],
    [".playbook-meta", "2xs", "11.5px → 11px"],
  ];

  for (const [selector, token, note] of SNAPS) {
    it(`${selector} (${note}) reads --pl-font-size-${token}`, () => {
      expect(rule(themeCss, selector)).toMatch(
        new RegExp(`font-size:\\s*var\\(--pl-font-size-${token}\\);`),
      );
    });
  }
});
