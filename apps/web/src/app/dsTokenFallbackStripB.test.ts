import { describe, expect, it } from "vitest";

// #3685 (part b): the pinned @protolabsai/design tokens are always loaded — main.tsx imports
// the DS before any app CSS — so a `var(--pl-…, #hex)` fallback can only ever paint the wrong,
// dark-only colour. The DS owner chose to DELETE them, and to retire the legacy --brand-*
// aliases onto real DS tokens (text → --pl-color-accent-fg, fill/border → --pl-color-accent).
// This pins the strip across the three sheets this card owns so a hex fallback or a --brand-
// reference can't creep back into them. Sibling dsTokenFallbackStrip.test.ts (part d2) guards
// settings/* + memory.css the same way.
//
// Source-level (Vite ?raw), same rationale as phantomTokenRename.test.ts: the DS token resolves
// to nothing at runtime under this harness, so a rendered getComputedStyle test would only ever
// observe the (now absent) fallback. The strip IS the change, so the source text is what we pin.
// Globs are compile-time and rooted at this file (src/app), so a file move is matched by suffix.

const SOURCES = import.meta.glob("../**/*.{css,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): same-dir files key as `./name`,
// siblings as `../dir/name`. Match by path suffix so a file move doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`source not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const count = (text: string, needle: string): number => text.split(needle).length - 1;

// The three files this card owns. Scoped deliberately: other sheets legitimately keep DS-token
// fallbacks (e.g. chat.css's `var(--pl-color-fg-muted, #8b8b93)` from #3682) and app-crash.css
// keeps its fallbacks by design, so this guard must NOT sweep the whole tree.
const TOUCHED = ["/theme.css", "/tools.css", "/ProtoLabsIcon.tsx"];

// A var() carrying a hex fallback: var(--pl-anything, #abc | #aabbcc | #aabbccdd). A color-mix
// operand like `var(--pl-color-accent), #fff 22%` has `)` before the comma, so it does NOT match;
// an rgba() fallback has no `#`, so it does not match either. Built from a RegExp string so this
// file never contains a bare offending literal and can't flag itself.
const HEX_FALLBACK = new RegExp("var\\(\\s*--pl-[\\w-]+\\s*,\\s*#[0-9a-fA-F]{3,8}");
// The retired legacy alias prefix, built by concat for the same reason.
const BRAND = "--" + "brand-";

function fallbackOffenders(suffix: string): string[] {
  return source(suffix)
    .split("\n")
    .map((line, i) => (HEX_FALLBACK.test(line) ? `${suffix}:${i + 1}` : null))
    .filter((hit): hit is string => hit !== null);
}

describe("DS token fallbacks + --brand- aliases stripped from theme.css, tools.css, ProtoLabsIcon.tsx (#3685 b)", () => {
  it("no var(--pl-…, #hex) fallback remains in any touched file", () => {
    for (const suffix of TOUCHED) {
      expect(fallbackOffenders(suffix), `hex fallback still present in ${suffix}`).toEqual([]);
    }
  });

  it("no legacy --brand-* reference remains in theme.css", () => {
    expect(source("/theme.css").includes(BRAND), `${BRAND} still referenced in theme.css`).toBe(false);
  });

  it("theme.css: retired --brand- fill/border sites now mix the bare DS accent", () => {
    const css = source("/theme.css");
    expect(css).toContain("background: var(--pl-color-accent);"); // .setup-progress span.done/.active
    expect(css).toContain("color-mix(in srgb, var(--pl-color-accent) 36%, transparent)"); // .setup-icon border
  });

  it("theme.css: retired --brand- text sites read the AA accent-text token", () => {
    // .metric svg, .setup-icon, .status-line svg (was --brand-violet-light) + .settings-help-link/
    // .setup-link (was --brand-indigo-bright) — all link/icon TEXT, readable on light and dark.
    expect(count(source("/theme.css"), "color: var(--pl-color-accent-fg);")).toBe(4);
  });

  it("ProtoLabsIcon.tsx: accent tone and gradient stops read the bare DS accent (mix operands kept)", () => {
    const tsx = source("/ProtoLabsIcon.tsx");
    expect(tsx).toContain('{ color: "var(--pl-color-accent)" }');
    expect(tsx).toContain('"color-mix(in srgb, var(--pl-color-accent), #fff 22%)"');
    expect(tsx).toContain('"color-mix(in srgb, var(--pl-color-accent), #000 25%)"');
  });

  it("sweeps real source text — a stubbed (empty) ?raw import would blind the guard", () => {
    // Guarded by vitest.config.ts `test.css.include` for CSS; if that regresses the ?raw import
    // returns "" and the sweeps above would pass on nothing. tsx is not stubbed.
    for (const suffix of TOUCHED) {
      expect(source(suffix).length, `${suffix} imported empty — widen test.css.include`).toBeGreaterThan(0);
    }
  });

  it("the hex-fallback pattern still bites (meta-guard, literals built by concat so this file never self-flags)", () => {
    const p = "--pl-color-";
    expect(HEX_FALLBACK.test("border-color: var(" + p + "accent, #7c8cff);")).toBe(true);
    expect(
      HEX_FALLBACK.test("background: color-mix(in srgb, var(" + p + "accent, #7c8cff) 22%, transparent);"),
    ).toBe(true);
    // The bare tokens this card leaves behind stay clean, and a color-mix operand hex is not a fallback.
    expect(HEX_FALLBACK.test("border-color: var(--pl-color-accent);")).toBe(false);
    expect(HEX_FALLBACK.test('stopColor: "color-mix(in srgb, var(--pl-color-accent), #fff 22%)"')).toBe(false);
  });
});
