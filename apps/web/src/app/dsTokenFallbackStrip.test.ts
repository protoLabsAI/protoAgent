import { describe, expect, it } from "vitest";

// #3685 (d2): the pinned @protolabsai/design tokens are always loaded — main.tsx imports the
// DS before any app CSS — so a `var(--pl-…, #hex)` fallback can only ever paint the wrong,
// dark-only colour. The DS owner chose to DELETE them, and to retire the legacy --brand-*
// aliases onto real DS tokens. This pins the strip across the four touched sheets so a hex
// fallback or a --brand- reference can't creep back into them.
//
// Source-level (Vite ?raw), same rationale as phantomTokenRename.test.ts: the DS ships from a
// private registry and isn't in node_modules here, so the real token resolves to nothing at
// runtime and a rendered getComputedStyle test would only ever observe the (now absent)
// fallback. The strip IS the change, so the source text is what we pin. Globs are compile-time
// and rooted at this file (src/app), so a file move is matched by suffix rather than churned.

const CSS_SOURCES = import.meta.glob("../**/*.css", {
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

const count = (text: string, needle: string): number => text.split(needle).length - 1;

// The four sheets this card owns. Scoped deliberately: other sheets legitimately keep DS-token
// fallbacks (e.g. chat.css's `var(--pl-color-fg-muted, #8b8b93)` from #3682), so this guard
// must NOT sweep the whole tree.
const TOUCHED = [
  "/settings/snapshot.css",
  "/settings/pathpicker.css",
  "/settings/settings.css",
  "/memory/memory.css",
];

// A var() carrying a hex fallback: var(--pl-anything, #abc | #aabbcc | #aabbccdd). An rgba()
// fallback (out of scope for this card) has no `#`, so it does not match. Built from a RegExp
// string so this file never contains a bare offending literal and can't flag itself.
const HEX_FALLBACK = new RegExp("var\\(\\s*--pl-[\\w-]+\\s*,\\s*#[0-9a-fA-F]{3,8}");
// The retired legacy alias prefix, built by concat for the same reason.
const BRAND = "--" + "brand-";

function fallbackOffenders(suffix: string): string[] {
  return source(suffix)
    .split("\n")
    .map((line, i) => (HEX_FALLBACK.test(line) ? `${suffix}:${i + 1}` : null))
    .filter((hit): hit is string => hit !== null);
}

describe("DS token fallbacks + --brand- aliases stripped from settings/* and memory.css (#3685 d2)", () => {
  it("no var(--pl-…, #hex) fallback remains in any touched sheet", () => {
    for (const suffix of TOUCHED) {
      expect(fallbackOffenders(suffix), `hex fallback still present in ${suffix}`).toEqual([]);
    }
  });

  it("no legacy --brand-* reference remains in any touched sheet", () => {
    for (const suffix of TOUCHED) {
      expect(source(suffix).includes(BRAND), `${BRAND} still referenced in ${suffix}`).toBe(false);
    }
  });

  it("the path-picker selection tint mixes the DS accent, not a --brand- alias", () => {
    expect(source("/settings/pathpicker.css")).toContain(
      "color-mix(in srgb, var(--pl-color-accent) 22%, transparent)",
    );
  });

  it("the settings dirty-row marker uses the DS accent", () => {
    expect(source("/settings/settings.css")).toContain(
      "box-shadow: inset 2px 0 0 var(--pl-color-accent);",
    );
  });

  it("snapshot.css keeps its five re-pointed tokens, now bare (no hex fallback)", () => {
    const snap = source("/settings/snapshot.css");
    expect(count(snap, "var(--pl-color-border)")).toBe(2); // section + source-tab dividers
    expect(count(snap, "var(--pl-color-status-warning)")).toBe(1); // credential-warning heading
    expect(count(snap, "var(--pl-color-accent)")).toBe(1); // active source-tab underline
    expect(count(snap, "var(--pl-color-status-error)")).toBe(1); // import error row
  });

  it("memory.css: all seven renamed muted-text sites now read the bare token", () => {
    const mem = source("/memory/memory.css");
    expect(count(mem, "var(--pl-color-fg-muted, #8a8f98)")).toBe(0);
    expect(count(mem, "var(--pl-color-fg-muted)")).toBe(8); // 7 stripped here + the injections-context site
  });

  it("sweeps real stylesheet text — a stubbed (empty) css import would blind the guard", () => {
    // Guarded by vitest.config.ts `test.css.include`; if that regresses, the ?raw import
    // returns "" and the sweeps above would pass on nothing.
    for (const suffix of TOUCHED) {
      expect(source(suffix).length, `${suffix} imported empty — widen test.css.include`).toBeGreaterThan(0);
    }
  });

  it("the hex-fallback pattern still bites (meta-guard, literals built by concat so this file never self-flags)", () => {
    const p = "--pl-color-";
    expect(HEX_FALLBACK.test("border: 1px solid var(" + p + "border, #2a2a31);")).toBe(true);
    expect(
      HEX_FALLBACK.test("background: color-mix(in srgb, var(" + p + "accent, #7c8cff) 22%, transparent);"),
    ).toBe(true);
    // The bare tokens this card leaves behind stay clean, and an rgba() fallback (out of scope) is not a hex.
    expect(HEX_FALLBACK.test("border: 1px solid var(--pl-color-border);")).toBe(false);
    expect(HEX_FALLBACK.test("background: var(--pl-color-bg-inset, rgba(255, 255, 255, 0.03));")).toBe(false);
  });
});
