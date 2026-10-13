import { describe, expect, it } from "vitest";

// #3688 part 6b — the docviewer + fleet type-scale migration. Every px font-size in these two
// stylesheets moves onto the DS scale var (--pl-font-size-*, from @protolabsai/design; the
// values are pinned by app/dsTypeScale.test.ts) with NO px fallback. This guard asserts on the
// raw stylesheet text (same source-guard pattern as chat/chat-css-tokens.test.ts): vitest.config
// opts all of src's CSS into processing (`test.css.include`) so `?raw` returns the real text.
import docviewerCss from "../docviewer/docviewer.css?raw";
import fleetCss from "../fleet/fleet.css?raw";

const FILES: Record<string, string> = {
  "docviewer.css": docviewerCss,
  "fleet/fleet.css": fleetCss,
};

// A bare px font-size — the exact shape this card eliminates. `font-size: 12px;`
const PX_FONT_SIZE = /font-size:\s*[0-9.]+px/;
// A --pl-font-size-* var that carries a fallback arm — the card mandates NO fallback, so
// `var(--pl-font-size-xs, 12px)` (or any fallback) must never appear.
const TOKEN_WITH_FALLBACK = /var\(\s*--pl-font-size-[a-z0-9]+\s*,/;

// The full multiset of font-size declarations each file must carry AFTER the migration, in
// document order. Every entry is a mapped scale step — no px, no fallback. If a value drifts or
// a new px site sneaks in, the extracted list stops matching and this fails loudly.
const EXPECTED: Record<string, readonly string[]> = {
  // .doc-viewer__subtitle (12→xs), .doc-viewer__body (14→base)
  "docviewer.css": ["var(--pl-font-size-xs)", "var(--pl-font-size-base)"],
  // .fleet-meta, .fleet-autostart-toggle (12→xs); .fleet-section-label (11→2xs);
  // .fleet-purge, .fleet-add-remote-title (13→sm); .fleet-add-remote .field > span,
  // .fleet-pair-form .field > span (12→xs); .fleet-pair-code (18→xl)
  "fleet/fleet.css": [
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-2xs)",
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-xl)",
  ],
};

function fontSizeValues(css: string): string[] {
  return Array.from(css.matchAll(/font-size:\s*([^;]+);/g), (m) => m[1].trim());
}

describe("#3688 6b — docviewer + fleet font-size migrated to DS scale tokens", () => {
  for (const [name, css] of Object.entries(FILES)) {
    it(`${name} is loaded as raw text (the guard is not silently blind)`, () => {
      expect(css.length).toBeGreaterThan(0);
    });

    it(`${name} carries no bare px font-size`, () => {
      expect(css).not.toMatch(PX_FONT_SIZE);
    });

    it(`${name} carries no --pl-font-size-* fallback arm`, () => {
      expect(css).not.toMatch(TOKEN_WITH_FALLBACK);
    });

    it(`${name} font-size declarations are exactly the mapped scale steps`, () => {
      expect(fontSizeValues(css)).toEqual(EXPECTED[name]);
    });

    it(`${name} keeps comments free of the glued \`*\` \`/\` minifier trap`, () => {
      // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a
      // comment early and silently drops downstream rules from the minified bundle.
      expect(css).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
    });
  }

  it("fleet keeps its focus-visible outline (a non-font-size site) untouched", () => {
    // The card explicitly leaves `.fleet-name-link:focus-visible` alone; assert its ring
    // survives so a future value-only sweep can't quietly fold it in.
    expect(fleetCss).toMatch(
      /\.fleet-name-link:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--pl-color-focus\)/,
    );
  });
});
