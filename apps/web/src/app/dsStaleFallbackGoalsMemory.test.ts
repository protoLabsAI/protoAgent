import { describe, expect, it } from "vitest";

// DS-adoption audit, rule stale-fallback (part 1f): main.tsx imports @protolabsai/design
// before any app CSS, and tokenNameGuard.test.ts proves every var(--pl-*) is defined by the
// installed DS. A literal fallback on a --pl token is therefore dead, drifted code — it can
// never paint. The DS owner chose to DROP the two literal fallbacks in these sheets:
//   goals.css   .goal-row:hover   var(--pl-color-bg-hover, rgba(127,127,127,0.06)) → var(--pl-color-bg-hover)
//   memory.css  .memory-detail-snippet  var(--pl-color-fg, inherit) → var(--pl-color-fg)
// This pins the strip for these two files ONLY: the wider DS-token migration is incremental,
// so a repo-wide sweep would (correctly) still find literal/nested fallbacks elsewhere and is
// out of scope here. Sibling dsFallbackDrop.test.ts / dsTokenFallbackStrip(B).test.ts guard
// the other audited sheets.
//
// Source-level (Vite ?raw), same rationale as phantomTokenRename.test.ts: the DS token resolves
// to nothing at runtime under this harness, so a rendered getComputedStyle test would only ever
// observe the (now absent) fallback. The strip IS the change, so the source text is what we pin.
// Globs are compile-time and rooted at this file (src/app); a move within src is still matched by
// path suffix in source() below.

const CSS_SOURCES = import.meta.glob("../{goals/goals.css,memory/memory.css}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): siblings key as `../dir/name`.
// Match by path suffix so a file move doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(CSS_SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`stylesheet not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const FILES = ["/goals/goals.css", "/memory/memory.css"];

// A --pl-* custom property read with a LITERAL fallback: `var(--pl-name, <literal>)` where the
// fallback does NOT itself start with `var(`. This deliberately still allows nested token
// fallbacks (`var(--pl-x, var(--pl-y))`), which the card leaves in place. Built from a RegExp
// string so this file never contains a bare offending literal and can't flag itself.
const LITERAL_FALLBACK = new RegExp("var\\(\\s*--pl-[a-z0-9-]+\\s*,(?!\\s*var\\()");

function literalFallbackOffenders(suffix: string): string[] {
  return source(suffix)
    .split("\n")
    .map((line, i) => (LITERAL_FALLBACK.test(line) ? `${suffix}:${i + 1}` : null))
    .filter((hit): hit is string => hit !== null);
}

describe("DS stale-fallback strip: goals.css + memory.css read bare var(--pl-*) (audit 1f)", () => {
  it("no var(--pl-…, <literal>) fallback remains in either sheet (nested token fallbacks still allowed)", () => {
    for (const f of FILES) {
      expect(literalFallbackOffenders(f), `literal fallback still present in ${f}`).toEqual([]);
    }
  });

  it("goals.css: the row hover reads the bare bg-hover token", () => {
    const css = source("/goals/goals.css");
    expect(css).toContain("background: var(--pl-color-bg-hover);");
    // The retired dark-only rgba fallback is gone (literal built by concat so this file is clean).
    expect(css).not.toContain("--pl-color-bg-hover," + " rgba(");
  });

  it("memory.css: the detail snippet reads the bare fg token", () => {
    const css = source("/memory/memory.css");
    expect(css).toContain("color: var(--pl-color-fg);");
    expect(css).not.toContain("--pl-color-fg," + " inherit");
  });

  it("sweeps real stylesheet text — a stubbed (empty) css import would blind the guard", () => {
    // Guarded by vitest.config.ts `css.include`; if that regresses, the ?raw import returns ""
    // and the sweeps above would pass on nothing.
    for (const f of FILES) {
      expect(
        source(f).length,
        `${f} imported empty — widen css.include in vitest.config.ts`,
      ).toBeGreaterThan(0);
    }
  });

  it("the literal-fallback pattern still bites (meta-guard; literals built by concat)", () => {
    const p = "--pl-color-";
    expect(LITERAL_FALLBACK.test("background: var(" + p + "bg-hover, rgba(127, 127, 127, 0.06));")).toBe(true);
    expect(LITERAL_FALLBACK.test("color: var(" + p + "fg, inherit);")).toBe(true);
    expect(LITERAL_FALLBACK.test("border-color: var(" + p + "accent, #7c8cff);")).toBe(true);
    // The bare tokens this card leaves behind stay clean, and a nested token fallback is kept.
    expect(LITERAL_FALLBACK.test("background: var(--pl-color-bg-hover);")).toBe(false);
    expect(LITERAL_FALLBACK.test("color: var(--pl-color-fg-subtle, var(--pl-color-fg-muted));")).toBe(false);
  });
});
