import { describe, expect, it } from "vitest";

// DS-adoption audit (rule stale-fallback, part 1b): the fleet-room / fleet-activity / work /
// hitl stylesheets carried `var(--pl-X, <literal>)` fallbacks — mono font stacks
// (`ui-monospace, "SF Mono", Menlo, monospace`) and popover-shadow literals
// (`0 2px 8px rgba(…)` / `0 -10px 28px -14px rgba(…)`). main.tsx imports @protolabsai/design
// before any app CSS and tokenNameGuard.test.ts (#3682) proves every `var(--pl-*)` resolves to
// an installed-DS token, so each such literal is dead, drifted code that can never paint. The
// DS owner's ruling is to DELETE the literal, keeping the bare `var(--pl-X)`. This pins the drop
// for the four files this card owns so a literal fallback can't creep back into them.
//
// Source-level (Vite ?raw), same rationale as dsFallbackDrop.test.ts / dsTokenFallbackStripB.test.ts:
// the DS token resolves to nothing under this harness, so a rendered getComputedStyle test would
// only ever observe the (now absent) fallback. The strip IS the change, so the source text is what
// we pin. The glob is compile-time, rooted at this file (src/app); scoping it to exactly these four
// basenames loads only them, not the whole src tree. The forbidden pattern is built from a RegExp
// string with `var(` split off, so this file holds no bare `var(--pl-…)` literal of its own.

const SOURCES = import.meta.glob(
  "../**/{fleet-activity.css,fleet-room.css,work.css,hitl.css}",
  { query: "?raw", import: "default", eager: true },
) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): same-dir files key as `./name`,
// siblings as `../dir/name`. Match by path suffix so a file move within src doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`stylesheet not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const FILES = [
  "/fleet-activity.css",
  "/fleet-room.css",
  "/work.css",
  "/chat/hitl.css",
];

// A `var(--pl-X, <literal>)` fallback: the token name, a comma, then a fallback that is NOT
// another `var(`. The negative lookahead sits DIRECTLY after the comma (`(?!\s*var\()`, its own
// `\s*` inside) so no outer `\s*` can backtrack past a leading space and defeat it — matching a
// font-family list (`var(--pl-font-mono, ui-monospace, …)`), a shadow literal
// (`var(--pl-shadow-popover, 0 …)`) or a hex fallback, but NEVER a token-to-token fallback
// (`var(--pl-color-bg-raised, var(--pl-color-bg))`) or a bare `var(--pl-X)`. Built from a string,
// with `var(` split from `--pl-`, so this file never contains a bare offending literal.
const LITERAL_FALLBACK = new RegExp("var" + "\\(\\s*--pl-[a-z0-9-]+\\s*,(?!\\s*var\\()");

function offenders(suffix: string): string[] {
  return source(suffix)
    .split("\n")
    .map((line, i) => (LITERAL_FALLBACK.test(line) ? `${suffix}:${i + 1}: ${line.trim()}` : null))
    .filter((hit): hit is string => hit !== null);
}

describe("stale-fallback 1b: fleet-room / fleet-activity / work / hitl drop literal var(--pl-X, …) fallbacks", () => {
  it("no var(--pl-X, <literal>) fallback remains in any of the four stylesheets", () => {
    for (const f of FILES) {
      expect(offenders(f), `literal fallback left in ${f}`).toEqual([]);
    }
  });

  it("keeps the bare mono / shadow tokens the card leaves behind (names unchanged)", () => {
    // Every dropped site becomes a fallback-free reference; spot-check one per token per file.
    expect(source("/fleet-activity.css")).toContain("font: 600 10px var(--pl-font-mono);");
    expect(source("/fleet-room.css")).toContain("font-family: var(--pl-font-mono);");
    expect(source("/fleet-room.css")).toContain("box-shadow: var(--pl-shadow-popover);");
    expect(source("/work.css")).toContain("box-shadow: var(--pl-shadow-popover);");
    expect(source("/chat/hitl.css")).toContain("box-shadow: var(--pl-shadow-popover);");
  });

  it("preserves fleet-room's token-to-token var() fallbacks (only literal fallbacks were dropped)", () => {
    // The @-mention popover's raised-surface + strong-border sites (#3169) are nested token
    // fallbacks, NOT literals — the audit rule leaves them untouched.
    const css = source("/fleet-room.css");
    expect(css).toContain("var(--pl-color-bg-raised, var(--pl-color-bg))");
    expect(css).toContain("var(--pl-color-border-strong, var(--pl-color-border))");
  });

  it("sweeps real stylesheet text — a stubbed (empty) css import would blind the guard", () => {
    // apps/web/src CSS is opted into processing by vitest.config.ts `css.include`; if that
    // regresses the ?raw import returns "" and the sweeps above would pass on nothing.
    for (const f of FILES) {
      expect(source(f).length, `${f} imported empty — widen css.include in vitest.config.ts`).toBeGreaterThan(0);
    }
  });

  it("the LITERAL_FALLBACK pattern still bites (meta-guard; literals built by concat)", () => {
    const V = "var" + "(";
    // Font stack, shadow literal and hex fallback are all caught.
    expect(LITERAL_FALLBACK.test('font: 500 11px ' + V + '--pl-font-mono, ui-monospace, "SF Mono", Menlo, monospace)')).toBe(true);
    expect(LITERAL_FALLBACK.test('box-shadow: ' + V + '--pl-shadow-popover, 0 2px 8px rgba(0, 0, 0, 0.22))')).toBe(true);
    expect(LITERAL_FALLBACK.test('color: ' + V + '--pl-color-accent, #7c8cff)')).toBe(true);
    // A bare token and a token-to-token fallback are NOT literal fallbacks.
    expect(LITERAL_FALLBACK.test('font-family: ' + V + '--pl-font-mono)')).toBe(false);
    expect(LITERAL_FALLBACK.test('background: ' + V + '--pl-color-bg-raised, ' + V + '--pl-color-bg))')).toBe(false);
  });
});
