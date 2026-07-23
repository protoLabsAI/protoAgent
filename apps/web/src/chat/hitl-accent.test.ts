import { describe, expect, it } from "vitest";

// Assert on the raw stylesheet text (same source-guard pattern as
// src/settings/devicesBind.test.ts / src/app/mobileBottomInset.test.ts). Vitest stubs CSS
// imports to empty modules by default, so `vitest.config.ts` opts this file into processing
// (`test.css.include`) — that's what lets `?raw` return its real text.
import hitlCss from "./hitl.css?raw";

// #2153 — the HITL card pinned its operator-facing accents to the literal brand token
// `var(--brand-indigo, #6366f1)`, so a workspace accent override (ThemePanel writes
// `--pl-color-accent` on <html>; theme-base.css bridges it into --accent/--brand-violet,
// which is why the composer's focus border follows it) recolored the composer but never the
// card. Every accent site now reads `var(--pl-color-accent, var(--brand-indigo, #6366f1))`
// — the semantic token layered over the literal fallback, so themed workspaces see the card
// follow their accent and unthemed installs render byte-identically to before.

// The exact chain every accent site must use. A lazy re-pin to the literal token during an
// unrelated edit fails the per-site assertion AND the whole-file sweep below.
const ACCENT = /var\(--pl-color-accent,\s*var\(--brand-indigo,\s*#6366f1\)\)/;

// Pull a single top-level rule's body by its exact line-start selector, so each assertion
// is scoped to its site (`.hitl-card` must not match `.hitl-float .hitl-card`).
function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(hitlCss);
  expect(match, `expected a \`${selector}\` rule in hitl.css`).not.toBeNull();
  return match![0];
}

describe("HITL card accents follow the workspace accent override (#2153)", () => {
  it("card border", () => {
    expect(rule(".hitl-card")).toMatch(new RegExp(`border:\\s*1px solid ${ACCENT.source}`));
  });

  it("active wizard step dot", () => {
    expect(rule('.hitl-dot[data-state="active"]')).toMatch(
      new RegExp(`background:\\s*${ACCENT.source}`),
    );
  });

  it("option card hover border", () => {
    expect(rule(".hitl-card-option:hover")).toMatch(
      new RegExp(`border-color:\\s*${ACCENT.source}`),
    );
  });

  it("option card focus-visible outline", () => {
    expect(rule(".hitl-card-option:focus-visible")).toMatch(
      new RegExp(`outline:\\s*2px solid ${ACCENT.source}`),
    );
  });

  it("selected option border AND color-mix fill", () => {
    const selected = rule(".hitl-card-option[data-selected]");
    expect(selected).toMatch(new RegExp(`border-color:\\s*${ACCENT.source}`));
    expect(selected).toMatch(new RegExp(`color-mix\\(in srgb,\\s*${ACCENT.source}\\s+12%`));
  });

  it("selection checkmark", () => {
    expect(rule(".hitl-card-mark")).toMatch(new RegExp(`color:\\s*${ACCENT.source}`));
  });

  it("all 7 sites are chained — no bare --brand-indigo pin anywhere in the file", () => {
    // Exactly the seven operator-facing sites: card border, active dot, option hover,
    // focus outline, selected border, selected fill, checkmark.
    expect(hitlCss.match(new RegExp(ACCENT.source, "g"))).toHaveLength(7);
    // Strip every properly-chained occurrence; any --brand-indigo left over is a re-pin
    // to the literal token without the semantic accent in front of it.
    const stripped = hitlCss.replace(new RegExp(ACCENT.source, "g"), "");
    expect(stripped).not.toContain("var(--brand-indigo");
  });

  it("keeps every touched CSS comment free of the glued `*` `/` minifier trap", () => {
    // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a
    // comment early and silently drops downstream rules from the minified bundle.
    expect(hitlCss).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
  });
});
