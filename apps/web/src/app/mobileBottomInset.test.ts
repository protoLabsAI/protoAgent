import { describe, expect, it } from "vitest";

// Assert on the raw stylesheet text (same source-guard pattern as src/settings/devicesBind.test.ts).
// Vitest stubs CSS imports to empty modules by default, so `vitest.config.ts` opts these two
// files into processing (`test.css.include`) — that's what lets `?raw` return their real text.
import mobileShellCss from "./mobile-shell.css?raw";
import themeCss from "./theme.css?raw";

// #2086 — the mobile shell reserved the TOP safe-area inset (`.mshell-head`) but nothing
// reserved the bottom, so on a device with a home indicator the last content row sat flush
// against / under the indicator. The fix inserts `padding-bottom: max(env(safe-area-inset-bottom),
// 12px)` on the two real mobile scroll/surface regions: the chat surface (mobile-shell.css,
// scoped by the `.mshell` ancestor) and the pushed rail/plugin scroll body `.stage-body`
// (theme.css, inside the `@media (max-width: 767px)` breakpoint only).

// The exact inset value shared by both rules. `max(…, 12px)` keeps the pre-fix 12px gutter on
// devices with no home indicator and grows to clear the indicator where present.
const INSET = /padding-bottom:\s*max\(\s*env\(safe-area-inset-bottom\)\s*,\s*12px\s*\)/;

// Return the body of a top-level `@media` block by brace-matching, so assertions can be scoped
// to "inside the mobile breakpoint" vs "everywhere else".
function mediaBlock(css: string, header: string): string {
  const start = css.indexOf(header);
  if (start === -1) return "";
  const open = css.indexOf("{", start);
  if (open === -1) return "";
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}" && --depth === 0) return css.slice(open + 1, i);
  }
  return css.slice(open + 1);
}

describe("mobile bottom safe-area inset (#2086)", () => {
  it("insets the chat surface so the composer clears the home indicator", () => {
    // `.mshell …` is scoped by the shell ancestor, which only mounts under 768px.
    const rule = /\.mshell\s+\.chat-session-slot\s*\{[^}]*\}/.exec(mobileShellCss);
    expect(rule, "expected a `.mshell .chat-session-slot` rule in mobile-shell.css").not.toBeNull();
    expect(rule![0]).toMatch(INSET);
  });

  it("insets the pushed rail/plugin scroll body (.stage-body) at the mobile breakpoint", () => {
    const mobile = mediaBlock(themeCss, "@media (max-width: 767px)");
    expect(mobile, "expected an @media (max-width: 767px) block in theme.css").not.toBe("");
    const rule = /\.stage-body\s*\{[^}]*\}/.exec(mobile);
    expect(rule, "expected a `.stage-body` rule inside @media (max-width: 767px)").not.toBeNull();
    expect(rule![0]).toMatch(INSET);
  });

  it("leaves desktop untouched — the bottom inset lives only under the mobile breakpoint", () => {
    // theme.css must not apply a bottom safe-area inset outside the mobile media query.
    const mobile = mediaBlock(themeCss, "@media (max-width: 767px)");
    const outsideMobile = themeCss.replace(mobile, "");
    expect(outsideMobile).not.toMatch(/padding-bottom:\s*max\(\s*env\(safe-area-inset-bottom\)/);
  });

  it("keeps every touched CSS comment free of the glued `*` `/` minifier trap", () => {
    // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a comment
    // early and silently drops downstream rules from the minified bundle.
    const glued = /[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/;
    expect(mobileShellCss).not.toMatch(glued);
    expect(themeCss).not.toMatch(glued);
  });
});
