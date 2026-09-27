import { describe, expect, it } from "vitest";

// Assert on the raw stylesheet text (same source-guard pattern as
// src/settings/devicesBind.test.ts / src/app/mobileBottomInset.test.ts). Vitest stubs CSS
// imports to empty modules by default, so `vitest.config.ts` opts this file into processing
// (`test.css.include`) — that's what lets `?raw` return its real text.
import hitlCss from "./hitl.css?raw";

// #2153 — the HITL card's operator-facing accents follow the workspace accent: they read the
// semantic `--pl-color-accent` the ThemePanel override writes on <html> (theme-base.css
// bridges the same token into the composer's focus border, which is why the card and the
// composer recolour together). No literal fallback: the DS tokens are always loaded, so a
// fallback could only paint a wrong, dark-only colour.

// The exact form every accent site must use. A lazy re-pin to a literal token during an
// unrelated edit fails the per-site assertion AND the whole-file sweep below.
const ACCENT = /var\(--pl-color-accent\)/;

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

  it("exactly 7 accent sites read the bare semantic token", () => {
    // The seven operator-facing sites: card border, active dot, option hover, focus outline,
    // selected border, selected fill, checkmark.
    expect(hitlCss.match(new RegExp(ACCENT.source, "g"))).toHaveLength(7);
  });

  it("no legacy brand alias remains anywhere in the file", () => {
    // Built by concatenation so this test file carries no such literal itself (a later card
    // adds a repo-wide guard for it). The legacy aliases were retired to DS tokens.
    const legacyPrefix = "--brand" + "-";
    expect(hitlCss).not.toContain(legacyPrefix);
  });

  it("keeps every touched CSS comment free of the glued `*` `/` minifier trap", () => {
    // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a
    // comment early and silently drops downstream rules from the minified bundle.
    expect(hitlCss).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
  });
});
