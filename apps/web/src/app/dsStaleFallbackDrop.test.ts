import { describe, expect, it } from "vitest";

// DS audit stale-fallback 1a: main.tsx imports @protolabsai/design before any app CSS, and
// tokenNameGuard.test.ts already proves every var(--pl-…) in apps/web/src resolves to an
// installed token. A literal fallback in `var(--pl-X, <literal>)` is therefore dead code —
// and many had drifted from the DS values (status-alias hexes, --pl-space-4 / --pl-radius px
// stand-ins, mono font stacks). This pins the drop for the four sheets this card rewrote:
// theme-base.css (the status compat aliases keep their names, lose the hex), theme.css,
// fleet.css and devices.css. NESTED `var(--pl-a, var(--pl-b))` fallbacks are legitimate and
// stay; app-crash.css is the deliberately-exempt error-boundary fallback and is untouched.
// A later guard card makes this rule tree-wide.
//
// Source-level (Vite ?raw), not a rendered getComputedStyle assertion, and for the same
// no-node-types reason as statusTokenGuard.test.ts / tokenNameGuard.test.ts. `var(` is split
// from `--pl-` in every literal below so this file never contains a bare `var(--pl-…)` and
// can't self-flag against the tree-wide phantom/name sweeps that also scan .ts sources.

const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): same-dir files key as
// `./name`, siblings as `../dir/name`. Match by path suffix (leading slash) so it resolves
// either way and a file move doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(CSS_SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`stylesheet not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const V = "var("; // split from --pl- so this file holds no bare var(--pl-…) literal

// The four sheets this card rewrote.
const AUDITED = [
  "/theme-base.css",
  "/theme.css",
  "/fleet/fleet.css",
  "/settings/devices.css",
];

// A --pl-* token read with a LITERAL fallback: `var(--pl-name, <not-a-var()>)`. The negative
// lookahead skips a nested var() fallback (`var(--pl-a, var(--pl-b))`, legitimate), so this
// matches hex, px, `transparent`, `inherit` and font-family stacks but not nested tokens.
const STALE_SRC = "var\\(\\s*--pl-[a-z0-9-]+\\s*,(?!\\s*var\\()";
const hasStale = (line: string): boolean => new RegExp(STALE_SRC).test(line);

describe("DS audit stale-fallback 1a: theme-base/theme/fleet/devices drop literal var() fallbacks", () => {
  it("no literal var(--pl-…, <literal>) fallback remains in the four audited stylesheets", () => {
    for (const f of AUDITED) {
      const offenders = source(f)
        .split("\n")
        .map((line, i) => ({ line, i }))
        .filter(({ line }) => hasStale(line))
        .map(({ i }) => `${f}:${i + 1}`);
      expect(offenders, `literal fallback left in ${f}`).toEqual([]);
    }
  });

  it("the five status compat aliases survive in theme-base.css, now fallback-free", () => {
    const themeBase = source("/theme-base.css");
    const aliases: Array<[string, string]> = [
      ["--success", "--pl-color-status-success"],
      ["--warning", "--pl-color-status-warning"],
      ["--error", "--pl-color-status-error"],
      ["--danger", "--pl-color-status-error"],
      ["--info", "--pl-color-status-info"],
    ];
    for (const [name, token] of aliases) {
      expect(themeBase, `${name} alias must survive fallback-free`).toContain(
        `${name}: ` + V + `${token});`,
      );
    }
    // The drifted, dark-only hex fallbacks are gone (built by concat; this file is a test
    // fixture but keeps no bare hex literal regardless).
    for (const hex of ["41c48d", "d4bd4f", "f07458", "7aa2ff"]) {
      expect(themeBase.includes("#" + hex), `stale hex #${hex} still in theme-base.css`).toBe(false);
    }
  });

  it("the rewritten sites read the bare token", () => {
    const theme = source("/theme.css");
    expect(theme, "mcp-json mono stack must be fallback-free").toContain(
      "font-family: " + V + "--pl-font-mono);",
    );
    expect(theme, "mcp-catalog dialog height must use bare --pl-space-4").toContain(
      V + "--pl-space-4)",
    );

    const fleet = source("/fleet/fleet.css");
    expect(fleet).toContain("border-radius: " + V + "--pl-radius);");
    expect(fleet).toContain("background: " + V + "--pl-color-bg-raised);");
    expect(fleet).toContain("color: " + V + "--pl-color-fg-muted);");
    expect(fleet, "fleet pair-code mono stack must be fallback-free").toContain(
      "font-family: " + V + "--pl-font-mono);",
    );

    const devices = source("/settings/devices.css");
    expect(devices, "devices-code mono stack must be fallback-free").toContain(
      "font-family: " + V + "--pl-font-mono);",
    );
  });

  it("leaves the legitimate nested var() fallbacks in devices.css alone", () => {
    const devices = source("/settings/devices.css");
    expect(devices).toContain(V + "--pl-color-bg-raised, " + V + "--pl-color-bg))");
    expect(devices).toContain(V + "--pl-color-border-strong, " + V + "--pl-color-border))");
  });

  it("app-crash.css is exempt and keeps its load-bearing literal fallbacks", () => {
    const crash = source("/app-crash.css");
    // The error boundary must still render if the token stylesheet failed to load.
    expect(crash, "app-crash.css must retain a literal fallback").toContain(
      V + "--pl-font-mono, ui-monospace",
    );
    expect(crash.split("\n").some(hasStale), "app-crash.css must keep its exempt fallbacks").toBe(true);
  });

  it("sweeps real stylesheet text — a stubbed (empty) css import would blind the guard", () => {
    // Guarded by vitest.config.ts `css.include`; if that regresses the ?raw import returns ""
    // and the sweeps above would pass on nothing.
    for (const f of [...AUDITED, "/app-crash.css"]) {
      expect(
        source(f).length,
        `${f} imported empty — widen css.include in vitest.config.ts`,
      ).toBeGreaterThan(0);
    }
  });

  it("the pattern still bites (meta-guard; forbidden literals built by concat)", () => {
    // Literal fallbacks of every shape this card removed are flagged.
    expect(hasStale(V + "--pl-space-4, 16px)")).toBe(true);
    expect(hasStale(V + "--pl-radius, 8px)")).toBe(true);
    expect(hasStale("background: " + V + "--pl-color-bg-raised, transparent)")).toBe(true);
    expect(hasStale("color: " + V + "--pl-color-fg-muted, inherit)")).toBe(true);
    expect(hasStale("font-family: " + V + "--pl-font-mono, ui-monospace, monospace)")).toBe(true);
    expect(hasStale(V + "--pl-color-status-success, #" + "41c48d)")).toBe(true);
    // Bare tokens and legitimate nested var() fallbacks stay clean.
    expect(hasStale(V + "--pl-radius)")).toBe(false);
    expect(hasStale("font-family: " + V + "--pl-font-mono)")).toBe(false);
    expect(hasStale(V + "--pl-color-bg-raised, " + V + "--pl-color-bg))")).toBe(false);
  });
});
