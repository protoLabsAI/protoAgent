import { describe, expect, it } from "vitest";

// #3688 part 7a — the DS type scale. These four Settings stylesheets migrated every in-range
// font-size:<n>px literal to a bare --pl-font-size-* token: 10/11/12/13/14px map to
// 3xs/2xs/xs/sm/base, which the DS (@protolabsai/design) defines at those exact px — so no
// rendered size changed and the migration is theme-invariant. The two big pairing-code sites
// in devices.css (`.devices-code code` at 28px plus its `<=767px` 22px override) are a DS GAP
// with no token and stay as literal px on purpose.
//
// Assert on the raw stylesheet text (same source-guard pattern as
// workflows/workflows-token-fallbacks.test.ts and app/statusTokenGuard.test.ts). Vitest stubs
// CSS imports to empty modules by default; vitest.config.ts opts all of src/**/*.css into
// processing so `?raw` returns the real text.
import pluginsCss from "./plugins.css?raw";
import snapshotCss from "./snapshot.css?raw";
import devicesCss from "./devices.css?raw";
import keybindingsCss from "./keybindings.css?raw";

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

describe("Settings type scale → DS tokens (#3688 part 7a)", () => {
  const files: Array<[string, string]> = [
    ["plugins.css", pluginsCss],
    ["snapshot.css", snapshotCss],
    ["devices.css", devicesCss],
    ["keybindings.css", keybindingsCss],
  ];

  it("imports the real stylesheet text, not empty stubs", () => {
    // If vitest.config.ts css.include ever stops covering src, these go empty and every guard
    // below would pass vacuously. Fail loud instead.
    for (const [name, css] of files) {
      expect(css.length, `${name} imported empty — check vitest.config.ts css.include`).toBeGreaterThan(100);
    }
  });

  it("plugins/snapshot/keybindings.css have zero literal px font-size sites", () => {
    for (const [name, css] of [
      ["plugins.css", pluginsCss],
      ["snapshot.css", snapshotCss],
      ["keybindings.css", keybindingsCss],
    ] as const) {
      expect(pxLines(css, name)).toEqual([]);
    }
  });

  it("devices.css keeps ONLY the two .devices-code pairing-code px sites (DS gap)", () => {
    // The 28px display and its `<=767px` 22px override both live on `.devices-code code`, have
    // no DS token, and must stay literal px. Nothing else in the file may carry a px font-size.
    const offenders = pxLines(devicesCss, "devices.css");
    expect(offenders.length).toBe(2);
    expect(offenders.some((l) => l.includes("28px"))).toBe(true);
    expect(offenders.some((l) => l.includes("22px"))).toBe(true);
    expect(devicesCss).toContain("font-size: 28px;");
    expect(devicesCss).toContain("font-size: 22px;");
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
    expect(tokenCount(pluginsCss)).toBe(15);
    expect(tokenCount(snapshotCss)).toBe(11);
    expect(tokenCount(devicesCss)).toBe(6);
    expect(tokenCount(keybindingsCss)).toBe(7);
  });

  it("maps each source px to its exact-px DS token (spot-check, one per token)", () => {
    // 10->3xs, 11->2xs, 12->xs, 13->sm, 14->base.
    expect(pluginsCss).toContain(".plugin-chip { font-size: var(--pl-font-size-3xs);"); // was 10px
    expect(pluginsCss).toContain(".plugin-ver { font-size: var(--pl-font-size-2xs);"); // was 11px
    expect(pluginsCss).toContain(".plugin-cell-contrib { font-size: var(--pl-font-size-xs);"); // was 12px
    expect(pluginsCss).toContain(".plugin-bundle-name { font-size: var(--pl-font-size-sm);"); // was 13px
    expect(pluginsCss).toContain(".plugin-card-head strong { font-size: var(--pl-font-size-base); }"); // was 14px
  });

  it("the px/token/fallback patterns still bite (meta-guard, literals built by concat)", () => {
    expect(PX_FONT_SIZE.test("font-size: " + "12px;")).toBe(true);
    expect(PX_FONT_SIZE.test("font-size: var(--pl-font-size-xs);")).toBe(false);
    expect(TOKEN_FONT_SIZE.test("font-size: var(--pl-font-size-2xs);")).toBe(true);
    expect(TOKEN_WITH_FALLBACK.test("font-size: var(--pl-font-size-sm" + ", 13px);")).toBe(true);
    expect(TOKEN_WITH_FALLBACK.test("font-size: var(--pl-font-size-sm);")).toBe(false);
  });
});
