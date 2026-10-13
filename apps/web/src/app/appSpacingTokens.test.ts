import { describe, expect, it } from "vitest";

import { PL_TOKEN_VARS } from "./PluginView";

// protoContent#547 (spacing half-steps), step 3: the off-scale spacing literals in
// app/palette.css and app/app-crash.css move onto @protolabsai/design 0.11.0's --pl-space-*
// tokens (incl. the 2px/6px/10px half-steps --pl-space-0_5/1_5/2_5).
//
//  • palette.css is a shipped sheet, so its one off-scale site (the 7px optical nudge on the
//    palette button, snapped up to the 8px grid step) reads a BARE token — the DS resolves at
//    runtime and the stale-fallback sweep forbids a px fallback on a shipped surface.
//  • app-crash.css is the root error-boundary crash screen (#872): it must still lay out even
//    if the DS token stylesheet never loaded, so — exactly like its colour reads — it keeps the
//    original px as the literal fallback. That is why it is the one file tokenNameGuard's
//    literal-fallback sweep exempts.
//
// Source-level (Vite ?raw), same approach and reason as phantomTokenRename.test.ts /
// tokenNameGuard.test.ts: the DS is absent from node_modules under jsdom, so the tokens
// resolve to nothing at runtime and a rendered getComputedStyle could never observe them.
// The tokenization IS the change, so the source is what we pin. Every var()/--pl- literal is
// assembled by concat (`V` splits `var(` from `--pl-`) so this file holds no bare var(--pl-…)
// that the tree-wide token sweep in tokenNameGuard.test.ts would capture.

const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app). Match by path suffix so a
// file move doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(CSS_SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`stylesheet not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const V = "var("; // split from `--pl-` so the tree-wide token sweep never captures this file
const count = (text: string, needle: string): number => text.split(needle).length - 1;

// Strip CSS block comments so a `7px` mentioned in prose isn't mistaken for a live value.
const stripCssComments = (text: string): string => text.replace(/\/\*[\s\S]*?\*\//g, "");

// A spacing declaration (property + shorthands/longhands the card covers) whose value carries
// one of the off-scale px literals this card retires (2/3/5/6/7/9/10/14). The negative lookbehind
// keeps `96px` from reading as `6px` and `110px` as `10px`; `\b` closes the number off.
const SPACING_PROP =
  "(?:padding|margin|gap|row-gap|column-gap|inset|inset-block|inset-inline|top|right|bottom|left)(?:-[a-z]+)*";
const OFFSCALE_PX = "(?<![\\w.])(?:2|3|5|6|7|9|10|14)px\\b";
const offScaleOnSpacing = new RegExp(SPACING_PROP + "\\s*:\\s*[^;{}]*" + OFFSCALE_PX);

describe("app spacing literals read --pl-space-* tokens (protoContent#547 step 3)", () => {
  it("references only real @protolabsai/design 0.11.0 space tokens (guards the DS bump)", () => {
    // The tokens this card lands. If a future DS bump drops one, catch it here — before the
    // tree-wide phantom sweep does — since each is referenced without a bare-token safety net.
    for (const n of [
      "--pl-space-1_5",
      "--pl-space-2",
      "--pl-space-2_5",
      "--pl-space-3",
      "--pl-space-6",
    ]) {
      expect(PL_TOKEN_VARS, `${n} missing — is @protolabsai/design 0.11.0 installed?`).toContain(n);
    }
  });

  it("the off-scale-spacing detector bites (meta-guard, so the sweep can't pass vacuously)", () => {
    // It flags an off-scale px on a spacing property …
    expect(offScaleOnSpacing.test("  padding-left: 7px;")).toBe(true);
    expect(offScaleOnSpacing.test("  gap: 10px;")).toBe(true);
    expect(offScaleOnSpacing.test("  margin-top: 6px;")).toBe(true);
    // … but clears the tokenized form, non-spacing px, the 1px allowance, and sizing that
    // merely embeds an off-scale digit (96px must not read as 6px, 340px not as 40/10px).
    expect(offScaleOnSpacing.test("  padding-left: " + V + "--pl-space-2);")).toBe(false);
    expect(offScaleOnSpacing.test("  font-size: 13px;")).toBe(false);
    expect(offScaleOnSpacing.test("  margin: -1px;")).toBe(false);
    expect(offScaleOnSpacing.test("  min-height: 96px;")).toBe(false);
    expect(offScaleOnSpacing.test("  --pa-cmdk-list-max: 340px;")).toBe(false);
  });

  it("palette.css: the palette-button nudge reads a bare --pl-space-2, no px fallback, no off-scale spacing literal", () => {
    const palette = source("/palette.css");
    // The 7px optical nudge snapped up to the 8px grid step, tokenized plainly (no fallback).
    expect(palette).toContain("padding-left: " + V + "--pl-space-2);");
    // No off-scale spacing literal survives on a spacing property (7px was the only site).
    expect(offScaleOnSpacing.test(stripCssComments(palette))).toBe(false);
    // A shipped sheet carries no px fallback on the tokenized site.
    expect(count(palette, V + "--pl-space-2, ")).toBe(0);
    // Out-of-scope sizing is untouched: the 1px sr-only box, its -1px margin, the list metrics.
    expect(palette).toContain("width: 1px;");
    expect(palette).toContain("margin: -1px;");
    expect(palette).toContain("min-height: 96px;");
    expect(palette).toContain("--pa-cmdk-list-max: 340px;");
  });

  it("app-crash.css: each spacing read is a token WITH its original px as the literal fallback", () => {
    const crash = source("/app-crash.css");
    // The four tokenized sites, token + px fallback (the crash screen's load-bearing shape).
    expect(crash).toContain("gap: " + V + "--pl-space-3, 12px);");
    expect(crash).toContain("padding: " + V + "--pl-space-6, 24px);");
    expect(crash).toContain("gap: " + V + "--pl-space-2_5, 10px);");
    expect(crash).toContain("margin-top: " + V + "--pl-space-1_5, 6px);");
    // No bare spacing literal remains outside var() fallback position.
    expect(crash).not.toContain("gap: 12px");
    expect(crash).not.toContain("padding: 24px");
    expect(crash).not.toContain("gap: 10px");
    expect(crash).not.toContain("margin-top: 6px");
  });

  it("app-crash.css: colour reads and font sizes are preserved (crash screen stays renderable)", () => {
    const crash = source("/app-crash.css");
    // The literal-fallback colour reads that make the crash screen render token-less are intact.
    expect(crash).toContain("background: " + V + "--pl-color-bg, #0a0a0c);");
    expect(crash).toContain("color: " + V + "--pl-color-fg, #ededed);");
    // Font sizes are out of scope for the spacing card and stay as bare px.
    expect(crash).toContain("font-size: 18px;");
    expect(crash).toContain("font-size: 13px;");
    expect(crash).toContain("font-size: 12px;");
  });
});
