import { describe, expect, it } from "vitest";

// #3688 part 4 — the DS type scale, sibling to the Settings parts (7a/7b). These four chat
// stylesheets migrated every font-size:<n>px literal to a bare --pl-font-size-* token. Each
// source px maps to the DS (@protolabsai/design) token defined at that exact px, so no rendered
// size changed and the migration is theme-invariant. Every site in these files is in the 9-18px
// range, so there is no DS gap here: zero literal px font-sizes survive. Several sites were
// half-pixels and snapped to the nearest token px (12.5 -> sm/13px, 10.5 -> 2xs/11px); a snap is
// a +0.5px size change, listed in the PR body.
//
// Assert on the raw stylesheet text (same source-guard pattern as
// settings/settings-type-scale-tokens-7b.test.ts and the accent guards in ./hitl-accent.test.ts).
// Vitest stubs CSS imports to empty modules by default; vitest.config.ts opts all of src/**/*.css
// into processing (`css.include`) so `?raw` returns the real text.
import chatCss from "./chat.css?raw";
import promptviewerCss from "./promptviewer.css?raw";
import chatComponentCss from "./chat-component.css?raw";
import hitlCss from "./hitl.css?raw";

// A `font-size: <n>px` literal (integer or half-pixel) — exactly what this migration removes.
const PX_FONT_SIZE = /font-size:\s*[0-9.]+px/;
// A font-size referencing a DS type-scale token — the shape the migration produces.
const TOKEN_FONT_SIZE = /font-size:\s*var\(--pl-font-size-(?:3xs|2xs|xs|sm|base|lg|xl)\)/;
// A token font-size that still carries a fallback arg, e.g. `var(--pl-font-size-sm, 13px)`.
// The card bans px fallbacks, so any comma inside the font-size var() is a violation.
const TOKEN_WITH_FALLBACK = /font-size:\s*var\(--pl-font-size-[a-z0-9]+\s*,/;

function pxLines(css: string, name: string): string[] {
  return css
    .split("\n")
    .map((line, i) => [i + 1, line] as const)
    .filter(([, line]) => PX_FONT_SIZE.test(line))
    .map(([n, line]) => `${name}:${n}: ${line.trim()}`);
}

function tokenCount(css: string): number {
  return (css.match(new RegExp(TOKEN_FONT_SIZE, "g")) ?? []).length;
}

describe("Chat type scale → DS tokens (#3688 part 4)", () => {
  const files: Array<[string, string]> = [
    ["chat.css", chatCss],
    ["promptviewer.css", promptviewerCss],
    ["chat-component.css", chatComponentCss],
    ["hitl.css", hitlCss],
  ];

  it("imports the real stylesheet text, not empty stubs", () => {
    // If vitest.config.ts css.include ever stops covering src, these go empty and every guard
    // below would pass vacuously. Fail loud instead.
    for (const [name, css] of files) {
      expect(css.length, `${name} imported empty — check vitest.config.ts css.include`).toBeGreaterThan(100);
    }
  });

  it("all four files have zero literal px font-size sites (every site was in-range)", () => {
    for (const [name, css] of files) {
      expect(pxLines(css, name)).toEqual([]);
    }
  });

  it("every DS-token font-size is bare (no px fallback)", () => {
    for (const [name, css] of files) {
      const withFallback = css
        .split("\n")
        .map((line, i) => [i + 1, line] as const)
        .filter(([, line]) => TOKEN_WITH_FALLBACK.test(line))
        .map(([n, line]) => `${name}:${n}: ${line.trim()}`);
      expect(withFallback).toEqual([]);
    }
  });

  it("migrated every in-range site to an in-scale token, at the expected per-file counts", () => {
    expect(tokenCount(chatCss)).toBe(15);
    expect(tokenCount(promptviewerCss)).toBe(12);
    expect(tokenCount(chatComponentCss)).toBe(8);
    expect(tokenCount(hitlCss)).toBe(7);
  });

  it("maps each integer source px to its exact-px DS token (spot-check)", () => {
    // 9 -> 3xs, 11 -> 2xs, 12 -> xs, 13 -> sm.
    expect(chatCss).toContain(".slash-kind {\n  flex: 0 0 auto;\n  font-size: var(--pl-font-size-3xs);"); // was 9px
    expect(hitlCss).toContain(".hitl-step-count {\n  font-size: var(--pl-font-size-2xs);"); // was 11px
    expect(promptviewerCss).toContain(".prompt-viewer__diff {\n  font-size: var(--pl-font-size-xs);"); // was 12px
    // chat-component .chat-comp: 13px -> sm.
    expect(chatComponentCss).toContain("padding: 10px 12px;\n  font-size: var(--pl-font-size-sm);"); // was 13px
  });

  it("snaps each former half-pixel site to its nearest token px", () => {
    // chat.css .chat-memory-note: 12.5px -> sm (13px).
    expect(chatCss).toContain(".chat-memory-note {\n  margin: 12px 0 0;\n  font-size: var(--pl-font-size-sm);");
    // chat.css .chat-usage-tip-sub: 10.5px -> 2xs (11px).
    expect(chatCss).toContain("font-weight: 400;\n  font-size: var(--pl-font-size-2xs);");
    // promptviewer.css .prompt-viewer__wire: 12.5px -> sm (13px).
    expect(promptviewerCss).toContain("padding: 8px 10px;\n  font-size: var(--pl-font-size-sm);");
  });

  it("leaves the HITL accent chain and the composer field outline suppression untouched", () => {
    // font-size is the ONLY property this card touches: the accent sites hitl-accent.test.ts pins
    // and the .pl-prompt__field outline suppression must survive verbatim.
    expect(hitlCss).toMatch(/\.hitl-card\s*\{[^}]*border:\s*1px solid var\(--pl-color-accent\)/);
    expect(chatCss).toContain(".pl-prompt__field:focus,\n.pl-prompt__field:focus-visible {\n  outline: none;\n}");
  });

  it("the px/token/fallback patterns still bite (meta-guard, literals built by concat)", () => {
    expect(PX_FONT_SIZE.test("font-size: " + "12.5px;")).toBe(true);
    expect(PX_FONT_SIZE.test("font-size: var(--pl-font-size-xs);")).toBe(false);
    expect(TOKEN_FONT_SIZE.test("font-size: var(--pl-font-size-2xs);")).toBe(true);
    expect(TOKEN_WITH_FALLBACK.test("font-size: var(--pl-font-size-sm" + ", 13px);")).toBe(true);
    expect(TOKEN_WITH_FALLBACK.test("font-size: var(--pl-font-size-sm);")).toBe(false);
  });
});
