import { describe, expect, it } from "vitest";

// #3685 (stale-fallback 1e): main.tsx imports @protolabsai/design before any app CSS, and
// app/tokenNameGuard.test.ts proves every var(--pl-*) is defined by the installed DS — so a
// LITERAL fallback in `var(--pl-X, <literal>)` is dead, drifted code that can never paint. This
// card stripped the literal fallbacks from the telemetry, schedule and snapshot sheets: the
// telemetry trace font (`var(--pl-font-mono)`), the schedule calendar hover states
// (`var(--pl-color-bg-hover)`) and the snapshot rows/caveat (`var(--pl-color-bg-inset)`).
//
// The sibling dsTokenFallbackStrip.test.ts (#3685 d2) guards only the `#hex` flavour on the
// settings/memory sheets; these three sites carried rgba()/monospace literals that guard
// deliberately ignores, so nothing locked the strip in. This pins it. Source-level (Vite ?raw),
// same rationale as its sibling: the DS ships from a private registry and isn't in node_modules
// here, so the token resolves to nothing at runtime and a rendered getComputedStyle test would
// only ever observe the (now absent) fallback — the source text IS the change. Globs are
// compile-time and rooted at this file (src/app), so a file move is matched by suffix, not churned.

const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app). Match by path suffix so a file
// move doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(CSS_SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`stylesheet not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const count = (text: string, needle: string): number => text.split(needle).length - 1;

// The three sheets this card owns.
const TOUCHED = ["/settings/telemetry.css", "/schedule/schedule.css", "/settings/snapshot.css"];

// A var(--pl-…) carrying a LITERAL fallback: `var(--pl-name, <arg>)` where <arg> is anything that
// is NOT another var(). A token-to-token fallback `var(--pl-a, var(--pl-b))` is intentionally kept
// by this card, so the fallback arg must not begin with `var(`. The whitespace lives INSIDE the
// negative lookahead so it can't backtrack to zero and let the lookahead pass at the space before
// a nested var(). Built from a RegExp string so this file never contains a bare offending literal.
const LITERAL_FALLBACK = new RegExp("var\\(\\s*--pl-[\\w-]+\\s*,(?!\\s*var\\()");

function offenders(suffix: string): string[] {
  return source(suffix)
    .split("\n")
    .map((line, i) => (LITERAL_FALLBACK.test(line) ? `${suffix}:${i + 1}: ${line.trim()}` : null))
    .filter((hit): hit is string => hit !== null);
}

describe("literal var(--pl-…, <literal>) fallbacks stripped from telemetry/schedule/snapshot (#3685 stale-fallback 1e)", () => {
  it("no var(--pl-…, <literal>) fallback remains in any touched sheet", () => {
    for (const suffix of TOUCHED) {
      expect(offenders(suffix), `literal token fallback still present in ${suffix}`).toEqual([]);
    }
  });

  it("the telemetry trace font reads the bare mono token", () => {
    expect(source("/settings/telemetry.css")).toContain("font-family: var(--pl-font-mono);");
  });

  it("the schedule calendar day hover state reads the bare hover token", () => {
    const sched = source("/schedule/schedule.css");
    expect(sched).toContain(".cal-day:hover { background: var(--pl-color-bg-hover); }");
  });

  it("both snapshot inset surfaces (row + caveat) read the bare inset token", () => {
    expect(count(source("/settings/snapshot.css"), "background: var(--pl-color-bg-inset);")).toBe(2);
  });

  it("sweeps real stylesheet text — a stubbed (empty) css import would blind the guard", () => {
    // Guarded by vitest.config.ts `test.css.include`; if that regresses, the ?raw import returns
    // "" and the sweep above would pass on nothing.
    for (const suffix of TOUCHED) {
      expect(source(suffix).length, `${suffix} imported empty — widen test.css.include`).toBeGreaterThan(0);
    }
  });

  it("the literal-fallback pattern bites literals but spares token-to-token fallbacks (meta-guard, literals built by concat)", () => {
    const v = "var(" + "--pl-";
    expect(LITERAL_FALLBACK.test("font-family: " + v + "font-mono, monospace);")).toBe(true);
    expect(LITERAL_FALLBACK.test("background: " + v + "color-bg-hover, rgba(127,127,127,.14));")).toBe(true);
    expect(LITERAL_FALLBACK.test("background: " + v + "color-bg-inset, rgba(255, 255, 255, 0.03));")).toBe(true);
    // The bare tokens this card leaves behind stay clean, and a token-to-token fallback is kept.
    expect(LITERAL_FALLBACK.test("font-family: " + v + "font-mono);")).toBe(false);
    expect(LITERAL_FALLBACK.test("color: " + v + "color-a, " + v + "color-b));")).toBe(false);
  });
});
