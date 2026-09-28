import { describe, expect, it } from "vitest";

// #3688 part 3 — the shell/mobile stylesheets (theme-base, mobile-shell, mobile-native, tools)
// moved every hard-coded px font-size onto the DS type scale (var(--pl-font-size-{3xs..xl}), no
// px fallback; the token values are pinned by app/dsTypeScale.test.ts against @protolabsai/design).
// This guard asserts on the raw stylesheet text (same source-guard pattern as
// app/dsTypeScaleSites2.test.ts) so a regression — a re-introduced px literal (in a rule OR a
// comment) or a token that grows a stray fallback arm — fails loudly here. It reads only the CSS
// text, never the DS package's token values. Vitest opts src's CSS into processing
// (vitest.config.ts `test.css.include`), which is what lets `?raw` return the real text, not "".
import mobileNativeCss from "./mobile-native.css?raw";
import mobileShellCss from "./mobile-shell.css?raw";
import themeBaseCss from "./theme-base.css?raw";
import toolsCss from "./tools.css?raw";

const FILES: Record<string, string> = {
  "app/theme-base.css": themeBaseCss,
  "app/mobile-shell.css": mobileShellCss,
  "app/mobile-native.css": mobileNativeCss,
  "app/tools.css": toolsCss,
};

// A bare px font-size — the exact shape this card eliminates, in rules AND in comments (the
// #3688 grep is comment-inclusive). `font-size: 12px` / `font-size: 12.5px`
const PX_FONT_SIZE = /font-size:\s*[0-9.]+px/;
// A --pl-font-size-* var that carries a fallback arm — the card mandates NO fallback, so
// `var(--pl-font-size-xs, 12px)` (or any fallback) must never appear.
const TOKEN_WITH_FALLBACK = /var\(\s*--pl-font-size-[a-z0-9]+\s*,/;
// A bare DS type-scale token: `var(--pl-font-size-<step>)` with NO comma-list fallback.
const BARE_TOKEN = /^var\(--pl-font-size-(?:3xs|2xs|xs|sm|base|lg|xl)\)$/;

// The full multiset of font-size declarations each file must carry AFTER the migration, in
// document order. Every entry is a mapped scale step — no px, no fallback. If a value drifts or
// a new px site sneaks in, the extracted list stops matching and this fails loudly.
const EXPECTED: Record<string, readonly string[]> = {
  // body (14→base), h1/h2 (14→base). The status aliases and the focus-visible outline rule
  // carry no font-size, so they are untouched and never appear here.
  "app/theme-base.css": ["var(--pl-font-size-base)", "var(--pl-font-size-base)"],
  // .mshell-title (15→lg, snapped +1px)
  "app/mobile-shell.css": ["var(--pl-font-size-lg)"],
  // input,textarea,select inside @media (hover: none) (16→lg). lg resolves to exactly 16px, so
  // the iOS focus-zoom guard still holds — it must NOT snap down.
  "app/mobile-native.css": ["var(--pl-font-size-lg)"],
  // .tools-name (12.5→sm, snapped +0.5px), .tools-desc (12→xs), .fs-projects-write (12→xs),
  // .fs-projects-warning (12→xs)
  "app/tools.css": [
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-xs)",
  ],
};

function fontSizeValues(css: string): string[] {
  return Array.from(css.matchAll(/font-size:\s*([^;]+);/g), (m) => m[1].trim());
}

describe("#3688 part 3 — shell/mobile font-size on DS tokens", () => {
  for (const [name, css] of Object.entries(FILES)) {
    it(`${name} is loaded as raw text (the guard is not silently blind)`, () => {
      expect(css.length).toBeGreaterThan(0);
    });

    it(`${name} carries no bare px font-size (comments included)`, () => {
      expect(css).not.toMatch(PX_FONT_SIZE);
    });

    it(`${name} carries no --pl-font-size-* fallback arm`, () => {
      expect(css).not.toMatch(TOKEN_WITH_FALLBACK);
    });

    it(`${name} every font-size is a bare --pl-font-size-* token`, () => {
      const values = fontSizeValues(css);
      expect(values.length, `expected at least one font-size in ${name}`).toBeGreaterThan(0);
      for (const value of values) {
        expect(value, `unexpected font-size \`${value}\` in ${name}`).toMatch(BARE_TOKEN);
      }
    });

    it(`${name} font-size declarations are exactly the mapped scale steps`, () => {
      expect(fontSizeValues(css)).toEqual(EXPECTED[name]);
    });

    it(`${name} keeps comments free of the glued \`*\` \`/\` minifier trap`, () => {
      // Mirror scripts/check-css-comments.mjs: a `*` `/` glued to identifier chars closes a
      // comment early and silently drops downstream rules from the minified bundle.
      expect(css).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
    });
  }
});

describe("#3688 part 3 — key sites read the mapped step", () => {
  // Pull one top-level rule's body by its exact line-start selector (each targeted rule is flat,
  // so `[^}]*` is a safe body matcher). Same helper as app/dsTypeScaleSites2.test.ts.
  function rule(css: string, selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
    expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
    return match![0];
  }

  it("theme-base.css body reads --pl-font-size-base (must compute to 14px — artifact-panel.spec.ts)", () => {
    expect(rule(themeBaseCss, "body")).toMatch(/font-size:\s*var\(--pl-font-size-base\);/);
  });

  it("theme-base.css h1,h2 reads --pl-font-size-base", () => {
    // h1/h2 is a grouped selector — assert the block that carries font-weight: 600.
    expect(themeBaseCss).toMatch(
      /h1,\s*h2\s*\{\s*font-size:\s*var\(--pl-font-size-base\);\s*font-weight:\s*600;/,
    );
  });

  it("theme-base.css keeps the focus-visible outline rule untouched (no font-size added)", () => {
    // The focus ring rule is out of scope for this card; it must stay a pure outline rule.
    const focusRule = rule(themeBaseCss, "button:focus-visible,\ninput:focus-visible,\ntextarea:focus-visible,\nselect:focus-visible");
    expect(focusRule).toMatch(/outline:\s*2px solid var\(--pl-color-focus\);/);
    expect(focusRule).not.toMatch(/font-size:/);
  });

  it("mobile-native.css input/textarea/select uses lg — the iOS focus-zoom guard (16px)", () => {
    // The rule is indented inside `@media (hover: none)`, so the line-start `rule()` helper can't
    // reach it; match the grouped selector block directly. lg resolves to exactly 16px, so the
    // guard against iOS zoom-on-focus (mobile.spec.ts) still holds — this must never snap down.
    expect(mobileNativeCss).toMatch(
      /input,\s*textarea,\s*select\s*\{\s*font-size:\s*var\(--pl-font-size-lg\);\s*\}/,
    );
  });
});

// The half-pixel / rounded sites that SNAPPED to the nearest step (size is theme-invariant):
// pin the direction of each snap by selector so a later re-round to the wrong step is caught.
describe("#3688 part 3 — snapped sites", () => {
  function rule(css: string, selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
    expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
    return match![0];
  }

  const SNAPS: Array<[css: string, file: string, selector: string, token: string, note: string]> = [
    [mobileShellCss, "mobile-shell.css", ".mshell-title", "lg", "15px → 16px"],
    [toolsCss, "tools.css", ".tools-name", "sm", "12.5px → 13px"],
  ];

  for (const [css, file, selector, token, note] of SNAPS) {
    it(`${file} ${selector} (${note}) reads --pl-font-size-${token}`, () => {
      expect(rule(css, selector)).toMatch(
        new RegExp(`font-size:\\s*var\\(--pl-font-size-${token}\\);`),
      );
    });
  }
});
