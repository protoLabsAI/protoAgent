import { describe, expect, it } from "vitest";

// DS audit `stale-fallback` (part 1d): main.tsx imports @protolabsai/design before any app CSS,
// and tokenNameGuard.test.ts proves every `var(--pl-…)` names a property the installed DS
// actually declares. So a *literal* fallback in `var(--pl-X, <literal>)` — a mono/sans font
// stack, `inherit`, `monospace` — is dead code: the token always resolves, so the fallback can
// never paint. This card strips those literals to a bare `var(--pl-X)` across the four sheets it
// owns, and this guard pins the strip so a literal fallback can't creep back into them.
//
// The load-bearing distinction: a NESTED token fallback `var(--pl-a, var(--pl-b))` is NOT dead —
// --pl-a may be absent while --pl-b is present (e.g. accent → fg, fg-subtle → fg-muted), so those
// stay. The offender pattern below matches a literal fallback but excludes a `var(` fallback.
//
// Source-level (Vite ?raw), same rationale as dsTokenFallbackStrip{,B}.test.ts: the DS ships from
// a private registry and isn't in node_modules here, so the real token resolves to nothing at
// runtime and a rendered getComputedStyle test would only ever observe the (now absent) fallback.
// The strip IS the change, so the source text is what we pin. Globs are compile-time and rooted at
// this file (src/app); each touched filename is unique under src, so a brace-scoped glob loads only
// these four and a move within src is still matched by suffix in source() below.

const SOURCES = import.meta.glob(
  "../**/{activity.css,identity.css,code-pane.css,IdentityPanel.tsx}",
  { query: "?raw", import: "default", eager: true },
) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): siblings key as `../dir/name`.
// Match by path suffix so a file move doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`source not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const count = (text: string, needle: string): number => text.split(needle).length - 1;

// The four files this card owns. Scoped deliberately: other sheets legitimately keep literal
// token fallbacks (and app-crash.css keeps its fallbacks by design, since the DS may not have
// loaded when it paints), so this guard must NOT sweep the whole tree.
const TOUCHED = [
  "/activity/activity.css",
  "/agent/identity.css",
  "/codeviewer/code-pane.css",
  "/agent/IdentityPanel.tsx",
];

// A var(--pl-X, <literal>) fallback: `var(` + a --pl token + `,` + a fallback that is NOT another
// `var(` (optional whitespace allowed). The `(?!\s*var\()` lookahead — with the whitespace kept
// INSIDE it, so `\s*` can't backtrack to zero and let the assertion pass on a mere space — is what
// spares the nested token fallbacks `var(--pl-a, var(--pl-b))` this card must preserve. A bare
// `var(--pl-X)` has `)` (not `,`) after the name, so it never matches. Built from a RegExp string
// so this file never contains a bare offending literal and can't self-flag under a future sweep.
const LITERAL_FALLBACK = new RegExp("var\\(\\s*--pl-[\\w-]+\\s*,(?!\\s*var\\()");

function fallbackOffenders(suffix: string): string[] {
  return source(suffix)
    .split("\n")
    .map((line, i) => (LITERAL_FALLBACK.test(line) ? `${suffix}:${i + 1}` : null))
    .filter((hit): hit is string => hit !== null);
}

describe("literal var(--pl-X, <fallback>) stripped from activity/identity/IdentityPanel/code-pane (DS audit stale-fallback 1d)", () => {
  it("no literal var(--pl-X, <literal>) fallback remains in any touched file", () => {
    for (const suffix of TOUCHED) {
      expect(fallbackOffenders(suffix), `literal fallback still present in ${suffix}`).toEqual([]);
    }
  });

  it("activity.css: the trigger mono stack is now bare, nested muted fallbacks preserved", () => {
    const css = source("/activity/activity.css");
    expect(css).toContain(".activity-trigger { font-family: var(--pl-font-mono); ");
    expect(count(css, "var(--pl-font-mono, ui-monospace, monospace)")).toBe(0);
    // The two nested token fallbacks (.activity-role, .activity-time) are NOT dead code — kept.
    expect(count(css, "var(--pl-color-fg-subtle, var(--pl-color-fg-muted))")).toBe(2);
  });

  it("identity.css: soul-preview sans + history-body mono are now bare, nested accent fallbacks preserved", () => {
    const css = source("/agent/identity.css");
    expect(css).toContain("font-family: var(--pl-font-sans);");
    expect(css).toContain("font-family: var(--pl-font-mono);");
    expect(count(css, "var(--pl-font-sans, inherit)")).toBe(0);
    expect(count(css, "var(--pl-font-mono, monospace)")).toBe(0);
    // The three nested accent→fg fallbacks (is-current border, badge bg + text) are kept.
    expect(count(css, "var(--pl-color-accent, var(--pl-color-fg))")).toBe(3);
  });

  it("code-pane.css: all mono-face sites are bare — four font-family + the pierre --diffs var", () => {
    const css = source("/codeviewer/code-pane.css");
    // 5 = 4 `font-family:` sites (__where, __recent-item, __file-row, __subnote code) + the pierre
    // `--diffs-font-family:` host var (whose text also contains the `font-family:` substring).
    expect(count(css, "var(--pl-font-mono)")).toBe(5);
    expect(count(css, "--diffs-font-family: var(--pl-font-mono);")).toBe(1);
    expect(count(css, "var(--pl-font-mono, ui-monospace, monospace)")).toBe(0);
  });

  it("IdentityPanel.tsx: soul textarea fontFamily is bare, fontSize (type-scale card 2b) untouched", () => {
    const tsx = source("/agent/IdentityPanel.tsx");
    expect(tsx).toContain('fontFamily: "var(--pl-font-mono)"');
    expect(tsx).toContain('fontSize: "13px"'); // owned by the type-scale card, must NOT be stripped
    expect(count(tsx, 'var(--pl-font-mono, monospace)')).toBe(0);
  });

  it("sweeps real source text — a stubbed (empty) ?raw import would blind the guard", () => {
    // Guarded by vitest.config.ts `test.css.include` for CSS; if that regresses the ?raw import
    // returns "" and the sweeps above would pass on nothing. The tsx is not stubbed.
    for (const suffix of TOUCHED) {
      expect(source(suffix).length, `${suffix} imported empty — widen test.css.include`).toBeGreaterThan(0);
    }
  });

  it("the literal-fallback pattern bites a literal but spares nested + bare (meta-guard, offenders built by concat)", () => {
    const m = "--pl-font-mono";
    expect(LITERAL_FALLBACK.test("font-family: var(" + m + ", monospace)")).toBe(true);
    expect(LITERAL_FALLBACK.test("font-family: var(" + m + ", ui-monospace, monospace)")).toBe(true);
    // A nested token fallback and a bare token are NOT literal fallbacks — the strip leaves both.
    expect(LITERAL_FALLBACK.test("color: var(--pl-color-fg-subtle, var(--pl-color-fg-muted))")).toBe(false);
    expect(LITERAL_FALLBACK.test("font-family: var(--pl-font-mono)")).toBe(false);
    // A color-mix operand token (comma sits after the `)`, not inside the var) is not a fallback.
    expect(LITERAL_FALLBACK.test("color-mix(in srgb, var(--pl-color-accent) 12%, transparent)")).toBe(false);
  });
});
