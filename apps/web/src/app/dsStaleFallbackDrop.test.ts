import { describe, expect, it } from "vitest";

// DS audit (stale-fallback): main.tsx imports @protolabsai/design before any app CSS, and
// tokenNameGuard.test.ts already proves every var(--pl-…) in apps/web/src resolves to an
// installed token (including --pl-motion-base/--pl-motion-ease). A literal fallback in
// `var(--pl-X, <literal>)` is therefore dead code — and many had drifted from the DS values
// (status-alias hexes, --pl-space-4 / --pl-radius px stand-ins, mono font stacks, an
// rgba() bg, a `160ms`/`ease` motion pair). This file pins the drop for the sheets two
// sibling audit cards rewrote:
//   • 1a — theme-base.css (the status compat aliases keep their names, lose the hex),
//     theme.css, fleet.css and devices.css.
//   • 1c — the four chat/* sheets: chat.css, chat-component.css, promptviewer.css and
//     tool-calls.css.
// NESTED `var(--pl-a, var(--pl-b))` fallbacks are a legitimate pattern the DS migration
// keeps and are never flagged; app-crash.css is the deliberately-exempt error-boundary
// fallback and is untouched. A later guard card makes this rule tree-wide.
//
// Source-level (Vite ?raw), not a rendered getComputedStyle assertion, and for the same
// no-node-types reason as statusTokenGuard.test.ts / tokenNameGuard.test.ts: the DS ships
// from a private registry and isn't in node_modules here, so the real token resolves to
// nothing at runtime and a rendered test would only ever observe the (now absent) fallback.
// The strip IS the change, so the source text is what we pin. `var(` is split from `--pl-`
// in every literal below so this file never contains a bare `var(--pl-…)` and can't
// self-flag against the tree-wide phantom/name sweeps that also scan .ts sources. Globs are
// compile-time and rooted at this file (src/app), so a file move is matched by suffix.

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

const count = (text: string, needle: string): number => text.split(needle).length - 1;

const V = "var("; // split from --pl- so this file holds no bare var(--pl-…) literal

// ===========================================================================================
// stale-fallback 1a — theme-base / theme / fleet / devices
// ===========================================================================================

// The four sheets card 1a rewrote.
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

  it("1a: sweeps real stylesheet text — a stubbed (empty) css import would blind the guard", () => {
    // Guarded by vitest.config.ts `css.include`; if that regresses the ?raw import returns ""
    // and the sweeps above would pass on nothing.
    for (const f of [...AUDITED, "/app-crash.css"]) {
      expect(
        source(f).length,
        `${f} imported empty — widen css.include in vitest.config.ts`,
      ).toBeGreaterThan(0);
    }
  });

  it("1a: the pattern still bites (meta-guard; forbidden literals built by concat)", () => {
    // Literal fallbacks of every shape 1a removed are flagged.
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

// ===========================================================================================
// stale-fallback 1c — chat / chat-component / promptviewer / tool-calls
// ===========================================================================================

// The four chat sheets card 1c owns. Scoped deliberately: other sheets legitimately keep
// DS-token fallbacks, so this guard must NOT sweep the whole tree.
const TOUCHED = [
  "/chat/chat.css",
  "/chat/chat-component.css",
  "/chat/promptviewer.css",
  "/chat/tool-calls.css",
];

// A `var(--pl-X, <literal>)` read whose fallback is a LITERAL (a font-stack, a keyword, an
// rgba(), a duration) — i.e. anything that is NOT another `var()`. The negative lookahead after
// the comma clears a nested token fallback (`var(--pl-a, var(--pl-b))`), which the DS migration
// keeps; the optional whitespace lives INSIDE the lookahead (`,(?!\s*var\()`) so the engine
// cannot backtrack a leading `\s*` to zero width and sidestep it.
const LITERAL_FALLBACK_SRC = "var\\(\\s*--pl-[\\w-]+\\s*,(?!\\s*var\\()";
const hasLiteralFallback = (line: string): boolean => new RegExp(LITERAL_FALLBACK_SRC).test(line);

function literalFallbackOffenders(suffix: string): string[] {
  return source(suffix)
    .split("\n")
    .map((line, i) => (hasLiteralFallback(line) ? `${suffix}:${i + 1}` : null))
    .filter((hit): hit is string => hit !== null);
}

describe("DS token literal fallbacks stripped from chat/* stylesheets (stale-fallback 1c, #3685)", () => {
  it("no var(--pl-X, <literal>) fallback remains in any touched sheet", () => {
    for (const suffix of TOUCHED) {
      expect(
        literalFallbackOffenders(suffix),
        `literal fallback still present in ${suffix}`,
      ).toEqual([]);
    }
  });

  it("chat-component.css: all three mono-stack sites now read the bare --pl-font-mono", () => {
    const css = source("/chat/chat-component.css");
    expect(count(css, V + "--pl-font-mono)")).toBe(3);
    expect(css).not.toContain("ui-monospace");
  });

  it("promptviewer.css: the mono-stack site now reads the bare --pl-font-mono", () => {
    const css = source("/chat/promptviewer.css");
    expect(count(css, V + "--pl-font-mono)")).toBe(1);
    expect(css).not.toContain("ui-monospace");
  });

  it("chat.css: the usage-tip font and the model-select hover background read bare tokens", () => {
    const css = source("/chat/chat.css");
    expect(css).toContain("font-family: " + V + "--pl-font-sans);");
    expect(css).not.toContain(", inherit)"); // no --pl-font-sans, inherit left
    expect(css).not.toContain("rgba(255, 255, 255, 0.03)"); // the model-select bg-raised fallback is gone
  });

  it("tool-calls.css: the tool-spotlight animation reads bare motion tokens", () => {
    const css = source("/chat/tool-calls.css");
    expect(css).toContain(V + "--pl-motion-base) " + V + "--pl-motion-ease)");
    expect(css).not.toContain("160ms");
    expect(css).not.toContain(", ease)");
  });

  it("1c: sweeps real stylesheet text — a stubbed (empty) css import would blind the guard", () => {
    // Guarded by vitest.config.ts `css.include`; if that regresses, the ?raw import returns ""
    // and the sweeps above would pass on nothing.
    for (const suffix of TOUCHED) {
      expect(source(suffix).length, `${suffix} imported empty — widen css.include`).toBeGreaterThan(0);
    }
  });

  it("1c: the literal-fallback pattern bites each stripped shape but clears nested/bare tokens (meta-guard, literals built by concat)", () => {
    // Each shape 1c removed, assembled so this file holds no bare `var(--pl-…, <literal>)`.
    expect(hasLiteralFallback("font-family: " + V + "--pl-font-mono, ui-monospace, monospace);")).toBe(true);
    expect(hasLiteralFallback("font-family: " + V + "--pl-font-sans, inherit);")).toBe(true);
    expect(hasLiteralFallback("background: " + V + "--pl-color-bg-raised, rgba(255, 255, 255, 0.03));")).toBe(true);
    expect(hasLiteralFallback("animation-duration: " + V + "--pl-motion-base, 160ms);")).toBe(true);
    expect(hasLiteralFallback("animation-timing-function: " + V + "--pl-motion-ease, ease);")).toBe(true);
    // A nested var() fallback and a fallback-free token both clear the rule.
    expect(hasLiteralFallback("color: " + V + "--pl-color-fg-subtle, " + V + "--pl-color-fg-muted))")).toBe(false);
    expect(hasLiteralFallback("font-family: " + V + "--pl-font-mono);")).toBe(false);
    // Prove the file:line shape a real hit would take over a two-line synthetic source.
    const synthetic = [
      "ok: " + V + "--pl-motion-base) " + V + "--pl-motion-ease);",
      "bad: " + V + "--pl-motion-base, 160ms);",
    ].join("\n");
    const hits: string[] = [];
    synthetic.split("\n").forEach((l, i) => {
      if (hasLiteralFallback(l)) hits.push(`src/synthetic.css:${i + 1}`);
    });
    expect(hits).toEqual(["src/synthetic.css:2"]);
  });
});
