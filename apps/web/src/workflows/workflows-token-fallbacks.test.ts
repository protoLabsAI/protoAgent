import { describe, expect, it } from "vitest";

// Assert on the raw stylesheet text (same source-guard pattern as
// app/statusTokenGuard.test.ts / app/solid-launcher-focus-ring.test.ts). Vitest stubs CSS
// imports to empty modules by default; `vitest.config.ts` opts all of `src/**/*.css` into
// processing so `?raw` returns the real text.
import workflowsCss from "./workflows.css?raw";

// #3685 part (a) — the DS (@protolabsai/design) tokens are always loaded (main.tsx imports
// the design system before any CSS, and #3682's guard proves every --pl-* name resolves), so
// a `var(--pl-name, #hex)` fallback can never paint anything but a wrong, dark-only colour.
// The DS owner's ruling was to DELETE the hex fallback, not correct it. This guard keeps the
// hex fallbacks from creeping back into workflows.css — scoped to THIS file, since the wider
// sweep across the other stylesheets lands in later parts of #3685.

// A `var(--pl-<token>, #<hex>)` fallback: the token name, a comma, then a hex literal. Built
// from a class so this test file never contains a bare literal that would flag itself.
const HEX_FALLBACK = new RegExp("var\\(--pl-[a-z0-9-]+,\\s*#[0-9a-fA-F]{3,8}\\)");
// Any hex colour literal at all — catches a "new hex literal" sneaking in even outside a var().
const ANY_HEX = /#[0-9a-fA-F]{3,8}\b/;
// A var() whose fallback is another --pl-* token, e.g.
// `var(--pl-color-fg-subtle, var(--pl-color-fg-muted))`. These are legit and MUST survive —
// the ruling only removes HEX fallbacks, not token-to-token ones.
const VAR_FALLBACK = /var\(--pl-[a-z0-9-]+,\s*var\(--pl-/;

describe("workflows.css carries no stale hex fallbacks (#3685 part a)", () => {
  it("the ?raw import returns the real stylesheet text, not an empty stub", () => {
    // If vitest.config.ts `test.css.include` ever stops covering src, this import goes empty
    // and the guard below would pass vacuously. Fail loud instead.
    expect(workflowsCss.length, "workflows.css imported empty — check vitest.config.ts test.css.include").toBeGreaterThan(100);
  });

  it("has zero `var(--pl-*, #hex)` fallback sites", () => {
    const offenders = workflowsCss
      .split("\n")
      .map((line, i) => [i + 1, line] as const)
      .filter(([, line]) => HEX_FALLBACK.test(line))
      .map(([n, line]) => `workflows.css:${n}: ${line.trim()}`);
    expect(offenders).toEqual([]);
  });

  it("contains no hex colour literal anywhere — no new hex was introduced", () => {
    expect(workflowsCss).not.toMatch(ANY_HEX);
  });

  it("still resolves every status/accent site to its bare --pl-* token (unchanged names)", () => {
    // These are the theme-responsive sites the card calls out — status dots, badges, borders,
    // status/accent text. Bare token references (no hex) are exactly what lets light mode
    // repaint them; a resurrected hex fallback would pin them dark-only.
    for (const token of [
      "var(--pl-color-status-success)", // builder-dot-ok, run-step-done, run-status-done, lane-chip-done
      "var(--pl-color-status-error)", //   builder-dot-err, run-step-failed, builder-toolchip-bad
      "var(--pl-color-status-warning)", // run-status-paused, run-step-gated, builder-errors, run-degraded
      "var(--pl-color-accent)", //         workflow-gate-count badge, builder-chip-on / dag-node-sel accent
      "var(--pl-color-bg-raised)", //      workflow-step / gate-card / run-step raised surfaces
    ]) {
      expect(workflowsCss, `expected a bare ${token} reference to survive`).toContain(token);
    }
  });

  it("preserves the token-to-token var() fallbacks (only HEX fallbacks were removed)", () => {
    // e.g. var(--pl-color-fg-subtle, var(--pl-color-fg-muted)) and the --pl-color-border-strong
    // / --pl-color-bg-hover / --pl-color-bg-inset chains — untouched by this change.
    expect(workflowsCss).toMatch(VAR_FALLBACK);
    expect(workflowsCss).toContain("var(--pl-color-border-strong, var(--pl-color-border))");
  });

  it("the HEX_FALLBACK pattern itself still bites (meta-guard, literal built by concat)", () => {
    expect(HEX_FALLBACK.test("background: var(" + "--pl-color-accent, " + "#7c8cff);")).toBe(true);
    expect(HEX_FALLBACK.test("color: var(--pl-color-status-warning);")).toBe(false);
    // A token-to-token fallback is NOT a hex fallback.
    expect(HEX_FALLBACK.test("var(--pl-color-fg-subtle, var(--pl-color-fg-muted))")).toBe(false);
  });
});
