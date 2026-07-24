import { describe, expect, it } from "vitest";

// Assert on the raw stylesheet text (same source-guard pattern as ../hitl-accent.test.ts,
// which guards the HITL card's accent chain in hitl.css). Vitest stubs CSS imports to empty
// modules by default, so `vitest.config.ts` opts chat.css into processing (`test.css.include`)
// — that's what lets `?raw` return its real text.
import chatCss from "../chat.css?raw";

// Success-toned system notes (noteToThread `noteTone:"success"`) are "the agent did the
// thing" confirmations, so their left accent follows the workspace accent — the exact
// `var(--pl-color-accent, var(--brand-indigo, #6366f1))` chain #2157 established for the
// HITL card — instead of pinning to literal success green. The other tones stay semantic
// on purpose: the #2197 export-blocked note relies on danger reading red, not accent.

// The exact chain every accent site must use (same regex as ../hitl-accent.test.ts). A lazy
// re-pin to `--pl-color-success` or bare `--brand-indigo` fails the success-rule assertion.
const ACCENT = /var\(--pl-color-accent,\s*var\(--brand-indigo,\s*#6366f1\)\)/;

// Pull a single top-level rule's body by its exact line-start selector, so each assertion
// is scoped to its own tone rule.
function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(chatCss);
  expect(match, `expected a \`${selector}\` rule in chat.css`).not.toBeNull();
  return match![0];
}

const note = (tone: string) =>
  rule(`.chat-session-slot .pl-message--system.chat-note--${tone} .pl-message__content`);

describe("success-toned system notes follow the workspace accent, other tones stay semantic", () => {
  it("success note border uses the accent chain, not success green", () => {
    expect(note("success")).toMatch(new RegExp(`border-left-color:\\s*${ACCENT.source}`));
    expect(note("success")).not.toContain("--pl-color-success");
  });

  it("info note keeps its semantic colour", () => {
    expect(note("info")).toMatch(/border-left-color:\s*var\(--pl-color-status-info\)/);
  });

  it("warning note keeps its semantic colour", () => {
    expect(note("warning")).toMatch(/border-left-color:\s*var\(--pl-color-status-warning\)/);
  });

  it("danger note keeps its semantic colour (the #2197 distinction is load-bearing)", () => {
    expect(note("danger")).toMatch(/border-left-color:\s*var\(--pl-color-status-error\)/);
  });

  it("keeps every chat.css comment free of the glued `*` `/` minifier trap", () => {
    // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a
    // comment early and silently drops downstream rules from the minified bundle.
    expect(chatCss).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
  });
});
