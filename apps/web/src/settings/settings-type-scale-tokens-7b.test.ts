import { describe, expect, it } from "vitest";

// #3688 part 7b — the DS type scale, sibling to part 7a. These four Settings stylesheets migrated
// every font-size:<n>px literal to a bare --pl-font-size-* token. Each source px maps to the DS
// (@protolabsai/design) token defined at that exact px, so no rendered size changed and the
// migration is theme-invariant. Every site in these files is in the 9-18px range, so there is no
// DS gap here: zero literal px font-sizes survive. Four sites were half-pixels and snapped to the
// nearest token px (12.5 -> sm/13px, 11.5 -> 2xs/11px); a snap is a ±0.5px size change, listed in
// the PR body.
//
// Assert on the raw stylesheet text (same source-guard pattern as
// settings/settings-type-scale-tokens.test.ts and app/statusTokenGuard.test.ts). Vitest stubs CSS
// imports to empty modules by default; vitest.config.ts opts all of src/**/*.css into processing so
// `?raw` returns the real text.
import pathpickerCss from "./pathpicker.css?raw";
import telemetryCss from "./telemetry.css?raw";
import providersCss from "./providers.css?raw";
import delegatesCss from "./delegates.css?raw";

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

describe("Settings type scale → DS tokens (#3688 part 7b)", () => {
  const files: Array<[string, string]> = [
    ["pathpicker.css", pathpickerCss],
    ["telemetry.css", telemetryCss],
    ["providers.css", providersCss],
    ["delegates.css", delegatesCss],
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
    expect(tokenCount(pathpickerCss)).toBe(5);
    expect(tokenCount(telemetryCss)).toBe(5);
    expect(tokenCount(providersCss)).toBe(3);
    expect(tokenCount(delegatesCss)).toBe(1);
  });

  it("maps each integer source px to its exact-px DS token (spot-check)", () => {
    // 11 -> 2xs, 12 -> xs, 13 -> sm.
    expect(delegatesCss).toContain(".delegate-field-help { font-size: var(--pl-font-size-2xs);"); // was 11px
    expect(providersCss).toContain(".provider-row__id {\n  font-size: var(--pl-font-size-xs);"); // was 12px
    expect(pathpickerCss).toContain("font: inherit;\n  font-size: var(--pl-font-size-sm);"); // was 13px
  });

  it("snaps each former half-pixel site to its nearest token px", () => {
    // telemetry .telemetry-table: 12.5px -> sm (13px).
    expect(telemetryCss).toContain("border-collapse: collapse;\n  font-size: var(--pl-font-size-sm);");
    // telemetry .trace-link/.trace-copy: 11.5px -> 2xs (11px).
    expect(telemetryCss).toContain("font-family: var(--pl-font-mono, monospace);\n  font-size: var(--pl-font-size-2xs);");
    // telemetry .insight-note: 11.5px -> 2xs (11px).
    expect(telemetryCss).toContain(".insight-note {\n  font-size: var(--pl-font-size-2xs);");
    // providers .providers-panel > .muted: 12.5px -> sm (13px).
    expect(providersCss).toContain("margin: 0 0 12px;\n  font-size: var(--pl-font-size-sm);");
  });

  it("the px/token/fallback patterns still bite (meta-guard, literals built by concat)", () => {
    expect(PX_FONT_SIZE.test("font-size: " + "12.5px;")).toBe(true);
    expect(PX_FONT_SIZE.test("font-size: var(--pl-font-size-xs);")).toBe(false);
    expect(TOKEN_FONT_SIZE.test("font-size: var(--pl-font-size-2xs);")).toBe(true);
    expect(TOKEN_WITH_FALLBACK.test("font-size: var(--pl-font-size-sm" + ", 13px);")).toBe(true);
    expect(TOKEN_WITH_FALLBACK.test("font-size: var(--pl-font-size-sm);")).toBe(false);
  });
});
