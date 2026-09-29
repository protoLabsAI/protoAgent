import { describe, expect, it } from "vitest";

// DS audit type-scale 2b — the rem/em sibling of chat-type-scale-tokens.test.ts (which pinned the
// px→scale migration, #3688 part 4). chat.css and tool-calls.css carried rem font-sizes that sat
// OFF the DS scale; every one is migrated to a bare --pl-font-size-* token per the audit mapping
// (1rem = 16px): 0.72–0.8rem → xs, 0.82–0.86rem → sm, 0.9rem → base. The SOLE survivor is the
// inline mono slash-command chip (.chat-user-text.chat-slash-cmd, 0.9em) — inline code the brief
// allows to stay em-relative — so this guard pins that as the one and only em font-size.
//
// Assert on the raw stylesheet text (same source-guard pattern as chat-type-scale-tokens.test.ts).
// vitest.config.ts opts all of src/**/*.css into processing (`css.include`) so `?raw` returns the
// real text; jsdom would otherwise stub CSS imports to empty modules.
import chatCss from "./chat.css?raw";
import toolCallsCss from "./tool-calls.css?raw";

// A `font-size: <n>rem` literal — exactly what this migration removes.
const REM_FONT_SIZE = /font-size:\s*[0-9.]+rem/;
// A `font-size: <n>em` literal. `[0-9.]+em` requires a digit immediately before "em", so it does
// NOT match "0.9rem" (the char before "em" there is "r"). This is the unit the migration keeps at
// exactly one site.
const EM_FONT_SIZE = /font-size:\s*[0-9.]+em/;
// A font-size referencing a DS type-scale token — the shape the migration produces.
const TOKEN_FONT_SIZE = /font-size:\s*var\(--pl-font-size-(?:3xs|2xs|xs|sm|base|lg|xl)\)/;

function matchingLines(css: string, name: string, re: RegExp): string[] {
  return css
    .split("\n")
    .map((line, i) => [i + 1, line] as const)
    .filter(([, line]) => re.test(line))
    .map(([n, line]) => `${name}:${n}: ${line.trim()}`);
}

describe("Chat rem/em font-sizes → DS type scale (DS audit type-scale 2b)", () => {
  const files: Array<[string, string]> = [
    ["chat.css", chatCss],
    ["tool-calls.css", toolCallsCss],
  ];

  it("imports the real stylesheet text, not empty stubs", () => {
    // If vitest.config.ts css.include ever stops covering src, these go empty and every guard
    // below would pass vacuously. Fail loud instead.
    for (const [name, css] of files) {
      expect(css.length, `${name} imported empty — check vitest.config.ts css.include`).toBeGreaterThan(100);
    }
  });

  it("has zero rem font-size literals in either file", () => {
    for (const [name, css] of files) {
      expect(matchingLines(css, name, REM_FONT_SIZE)).toEqual([]);
    }
  });

  it("keeps exactly one em font-size — the .chat-slash-cmd inline mono chip — and none elsewhere", () => {
    // tool-calls.css has no em font-size at all.
    expect(matchingLines(toolCallsCss, "tool-calls.css", EM_FONT_SIZE)).toEqual([]);
    // chat.css keeps precisely one, and it is the slash-command chip at 0.9em.
    const emSites = matchingLines(chatCss, "chat.css", EM_FONT_SIZE);
    expect(emSites).toHaveLength(1);
    expect(emSites[0]).toContain("font-size: 0.9em;");
    // …and that em lives on the slash-cmd selector, not some other rule.
    expect(chatCss).toMatch(/\.chat-user-text\.chat-slash-cmd\s*\{[^}]*font-size:\s*0\.9em/);
  });

  it("maps each converted site to its band's DS token (spot-check across the mapping)", () => {
    // 0.9rem → base
    expect(chatCss).toContain(".chat-scheduled-summary {\n  font-size: var(--pl-font-size-base);");
    // 0.82rem → sm
    expect(chatCss).toContain(".chat-server-result-label {\n  flex: 0 0 auto;\n  font-weight: 600;\n  font-size: var(--pl-font-size-sm);");
    // 0.72rem → xs
    expect(chatCss).toContain(".chat-delegation-kind {\n  flex: 0 0 auto;\n  padding: 0 6px;\n  font-size: var(--pl-font-size-xs);");
    // tool-calls.css 0.82rem → sm, 0.72rem → xs, 0.84rem → sm.
    expect(toolCallsCss).toContain(".reasoning-text {\n  white-space: pre-wrap;\n  word-break: break-word;\n  font-size: var(--pl-font-size-sm);");
    expect(toolCallsCss).toContain(".tool-cancel-btn {\n  display: inline-flex;\n  align-items: center;\n  gap: var(--pl-space-1);\n  padding: 1px 7px;\n  width: auto;\n  border-radius: 6px;\n  font-size: var(--pl-font-size-xs);");
    expect(toolCallsCss).toContain(".tool-calc {\n  display: flex;\n  align-items: baseline;\n  flex-wrap: wrap;\n  gap: 6px;\n  font-size: var(--pl-font-size-sm);");
  });

  it("produced only in-scale tokens for the migrated sites (counts match the 2b conversion)", () => {
    // 20 rem sites in chat.css (the em chip is untouched) + the 15 the px migration already
    // left, less the one `.chat-delegation-toggle` site removed when that hand-rolled control
    // was swapped to a DS Button (#551 action-button rule, card 2).
    expect((chatCss.match(new RegExp(TOKEN_FONT_SIZE, "g")) ?? []).length).toBe(34);
    // tool-calls.css had no prior token font-sizes; all 18 come from this migration.
    expect((toolCallsCss.match(new RegExp(TOKEN_FONT_SIZE, "g")) ?? []).length).toBe(18);
  });

  it("the rem/em patterns still bite (meta-guard, literals built by concat so this file self-passes)", () => {
    expect(REM_FONT_SIZE.test("font-size: " + "0.82rem;")).toBe(true);
    expect(REM_FONT_SIZE.test("font-size: var(--pl-font-size-sm);")).toBe(false);
    // The em pattern matches a bare em but NOT a rem literal.
    expect(EM_FONT_SIZE.test("font-size: " + "0.9em;")).toBe(true);
    expect(EM_FONT_SIZE.test("font-size: " + "0.9rem;")).toBe(false);
  });
});
