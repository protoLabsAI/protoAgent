import { describe, expect, it } from "vitest";

// #3685 part d1: the code viewer, activity feed and schedule builder stylesheets carried
// `var(--pl-…, #hex)` fallbacks and legacy --brand-* aliases. main.tsx imports
// @protolabsai/design before any app CSS, so the --pl-* tokens always resolve — a hex
// fallback could therefore only ever paint a dark-only wrong colour, never help — and the
// --brand-* aliases are retired to real DS tokens (scheduler → --pl-color-chart-series2,
// a2a → --pl-color-chart-series8, .cal-sel → --pl-color-accent / --pl-color-fg-on-accent).
// This pins the drop for these three files ONLY: the wider DS-token migration is
// incremental, so a repo-wide sweep would (correctly) still find fallbacks/aliases
// elsewhere and is out of scope here.
//
// Source-level (Vite ?raw), not a rendered getComputedStyle assertion, and for the same
// no-node-types reason, as statusTokenGuard.test.ts / phantomTokenRename.test.ts. The
// forbidden literals are built by concat so this file never contains a bare --brand- or a
// bare hex-fallback token and can't flag itself.

const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): siblings key as
// `../dir/name`. Match by path suffix so a file move doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(CSS_SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`stylesheet not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const FILES = [
  "/codeviewer/code-pane.css",
  "/activity/activity.css",
  "/schedule/schedule.css",
];

// A --pl-* custom property read with a hex fallback: `var(--pl-name, #hex)`. Does NOT
// match a fallback-free token, a nested var() fallback (`var(--pl-x, var(--pl-y))`), or a
// font-family list (`var(--pl-font-mono, ui-monospace, monospace)`) — the `#` after the
// comma is required.
const HEX_FALLBACK = new RegExp("var\\(--pl-[a-z0-9-]+\\s*,\\s*#[0-9a-fA-F]");
const BRAND = new RegExp("--" + "brand-");

describe("#3685 d1: code-pane / activity / schedule drop hex fallbacks + --brand-* aliases", () => {
  it("no var(--pl-…, #hex) fallback remains in any of the three stylesheets", () => {
    for (const f of FILES) {
      const offenders = source(f)
        .split("\n")
        .map((line, i) => ({ line, i }))
        .filter(({ line }) => HEX_FALLBACK.test(line))
        .map(({ i }) => `${f}:${i + 1}`);
      expect(offenders, `hex fallback left in ${f}`).toEqual([]);
    }
  });

  it("no --brand-* alias remains in any of the three stylesheets", () => {
    for (const f of FILES) {
      expect(BRAND.test(source(f)), `--brand- alias left in ${f}`).toBe(false);
    }
  });

  it("activity origins re-point: scheduler → chart-series2, a2a → chart-series8 (colour + color-mix border)", () => {
    const css = source("/activity/activity.css");
    // Each origin sets both `color` and the `border-color` color-mix = two references.
    expect(css.split("var(--pl-color-chart-series2)").length - 1).toBe(2);
    expect(css.split("var(--pl-color-chart-series8)").length - 1).toBe(2);
  });

  it("schedule .cal-sel rides the accent pair and no longer hardcodes white text", () => {
    const css = source("/schedule/schedule.css");
    expect(css).toContain(
      ".cal-sel { background: var(--pl-color-accent); color: var(--pl-color-fg-on-accent); border-color: transparent; }",
    );
    expect(css).toContain(".cal-sel:hover { background: var(--pl-color-accent); }");
    expect(css).not.toContain("#" + "fff");
  });

  it("sweeps real stylesheet text — a stubbed (empty) css import would blind the guard", () => {
    // Guarded by vitest.config.ts `test.css.include`; if that regresses, the ?raw import
    // returns "" and the sweeps above would pass on nothing.
    for (const f of FILES) {
      expect(
        source(f).length,
        `${f} imported empty — widen test.css.include in vitest.config.ts`,
      ).toBeGreaterThan(0);
    }
  });

  it("the guard patterns still bite (meta-guard; forbidden literals built by concat)", () => {
    expect(HEX_FALLBACK.test("var(" + "--pl-color-border, #" + "2a2a31)")).toBe(true);
    expect(HEX_FALLBACK.test("color-mix(in srgb, var(" + "--pl-color-accent, #" + "9b87f2) 22%")).toBe(true);
    expect(BRAND.test("color: var(--" + "brand-pink)")).toBe(true);
    // Real, fallback-free tokens and non-hex fallbacks stay clean.
    expect(HEX_FALLBACK.test("var(--pl-color-border)")).toBe(false);
    expect(HEX_FALLBACK.test("var(--pl-color-fg-subtle, var(--pl-color-fg-muted))")).toBe(false);
    expect(HEX_FALLBACK.test("var(--pl-font-mono, ui-monospace, monospace)")).toBe(false);
    expect(BRAND.test("var(--pl-color-chart-series2)")).toBe(false);
  });
});
