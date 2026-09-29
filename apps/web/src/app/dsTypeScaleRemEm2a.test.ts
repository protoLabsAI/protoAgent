import { describe, expect, it } from "vitest";

// DS audit `type-scale` (part 2a): fontSizeGuard (#3772) bans only px, so rem/em font-sizes sat
// off the DS scale. This card maps every rem/em font-size in theme.css, settings.css and
// memory.css onto a bare --pl-font-size-* step (1rem = 16px; 0.72–0.8rem → xs, 0.82–0.86rem → sm,
// 0.85em → sm, 0.8em → xs), and migrates IdentityPanel's inline 13px soul-textarea fontSize to sm.
// The .update-notice-ver rule additionally drops its literal `ui-monospace, monospace` stack for
// var(--pl-font-mono) — it renders inside the fixed-size DS Dialog title, so the `em` bought no
// scaling and the mono face is the DS token.
//
// Source-level (Vite ?raw), same rationale as app/dsTypeScaleThemeCssP1.test.ts: the DS
// (@protolabsai/design) ships from a private registry and isn't in node_modules here, so the token
// resolves to nothing at runtime and a getComputedStyle test would observe nothing. The text IS
// the change, so the raw source is what we pin. vitest.config.ts `test.css.include` opts src's CSS
// into processing, which is what lets `?raw` return the real text instead of "".
import themeCss from "./theme.css?raw";
import identityPanelTsx from "../agent/IdentityPanel.tsx?raw";
import memoryCss from "../memory/memory.css?raw";
import settingsCss from "../settings/settings.css?raw";

const CSS: Record<string, string> = {
  "app/theme.css": themeCss,
  "settings/settings.css": settingsCss,
  "memory/memory.css": memoryCss,
};

// A rem/em font-size literal — exactly what this card eliminates. `r?em` catches both `0.8rem`
// and `0.85em`; the trailing boundary keeps it off token names like `--pl-font-size-sm`.
const REM_EM_FONT_SIZE = /font-size:\s*[0-9.]+r?em\b/;
// A --pl-font-size-* var carrying a fallback arm — the scale is always loaded, so no fallback.
const TOKEN_WITH_FALLBACK = /var\(\s*--pl-font-size-[a-z0-9]+\s*,/;
// A bare DS type-scale token: `var(--pl-font-size-<step>)` with NO comma-list fallback.
const BARE_TOKEN = /^var\(--pl-font-size-(?:3xs|2xs|xs|sm|base|lg|xl)\)$/;

function fontSizeValues(css: string): string[] {
  return Array.from(css.matchAll(/font-size:\s*([^;]+);/g), (m) => m[1].trim());
}

// Pull one top-level flat rule's body by its exact line-start selector.
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
  expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
  return match![0];
}

describe("DS type-scale 2a — rem/em font-sizes → DS tokens", () => {
  for (const [name, css] of Object.entries(CSS)) {
    it(`${name} loads as raw text (the guard is not silently blind)`, () => {
      expect(css.length, `${name} imported empty — check vitest.config.ts css.include`).toBeGreaterThan(100);
    });

    it(`${name} carries no rem/em font-size`, () => {
      const offenders = css
        .split("\n")
        .map((line, i) => [i + 1, line] as const)
        .filter(([, line]) => REM_EM_FONT_SIZE.test(line))
        .map(([n, line]) => `${name}:${n}: ${line.trim()}`);
      expect(offenders).toEqual([]);
    });

    it(`${name} every --pl-font-size-* reference is bare (no fallback arm)`, () => {
      expect(css).not.toMatch(TOKEN_WITH_FALLBACK);
      const tokenValues = fontSizeValues(css).filter((v) => v.includes("--pl-font-size-"));
      expect(tokenValues.length, `expected a migrated DS type-scale font-size in ${name}`).toBeGreaterThan(0);
      for (const value of tokenValues) {
        expect(value, `unexpected token font-size \`${value}\` in ${name}`).toMatch(BARE_TOKEN);
      }
    });
  }
});

// Pin EVERY migrated single-selector site by selector so a later re-map to the wrong step is
// caught (the sweep above only proves no rem/em survives; these prove each landed on the right
// step). Values follow the card mapping: 0.72–0.8rem → xs, 0.82–0.86rem → sm, 0.85em → sm,
// 0.8em → xs. (theme.css's `.setup-link` and `.federated-*` sites were dead selectors,
// retired in #3863, so they are no longer pinned here.)
describe("DS type-scale 2a — each migrated site reads the mapped step", () => {
  const SITES: Array<[css: string, file: string, selector: string, token: string, note: string]> = [
    [themeCss, "theme.css", ".settings-status", "xs", "0.8rem → xs"],
    [themeCss, "theme.css", ".update-notice-cur", "xs", "0.8em → xs"],
    [settingsCss, "settings.css", ".settings-inline-status", "xs", "0.8rem → xs"],
    [settingsCss, "settings.css", ".setting-label", "sm", "0.86rem → sm"],
    [settingsCss, "settings.css", ".setting-desc", "xs", "0.76rem → xs"],
    [settingsCss, "settings.css", ".setting-override-note", "xs", "0.74rem → xs"],
    [settingsCss, "settings.css", ".setting-toggle", "xs", "0.8rem → xs"],
    [settingsCss, "settings.css", ".secrets-status-meta", "xs", "0.8rem → xs"],
    [settingsCss, "settings.css", ".secrets-status-vars code", "xs", "0.72rem → xs"],
    [memoryCss, "memory.css", ".memory-panel-hint", "sm", "0.82rem → sm"],
    [memoryCss, "memory.css", ".memory-row-title code", "xs", "0.8rem → xs"],
    [memoryCss, "memory.css", ".memory-row-topic", "sm", "0.85rem → sm"],
    [memoryCss, "memory.css", ".memory-row-meta", "xs", "0.75rem → xs"],
    [memoryCss, "memory.css", ".memory-session-pre", "sm", "0.82rem → sm"],
    [memoryCss, "memory.css", ".memory-detail-meta", "xs", "0.78rem → xs"],
    [memoryCss, "memory.css", ".memory-detail-group h4", "xs", "0.8rem → xs"],
    [memoryCss, "memory.css", ".memory-detail-group li", "sm", "0.85rem → sm"],
    [memoryCss, "memory.css", ".memory-detail-source", "xs", "0.78rem → xs"],
  ];

  for (const [css, file, selector, token, note] of SITES) {
    it(`${file} ${selector} (${note}) reads --pl-font-size-${token}`, () => {
      expect(rule(css, selector)).toMatch(
        new RegExp(`font-size:\\s*var\\(--pl-font-size-${token}\\);`),
      );
    });
  }

  it("theme.css .update-notice-ver reads sm AND swaps the literal mono stack for var(--pl-font-mono)", () => {
    const body = rule(themeCss, ".update-notice-ver");
    expect(body).toMatch(/font-size:\s*var\(--pl-font-size-sm\);/);
    expect(body).toMatch(/font-family:\s*var\(--pl-font-mono\);/);
    // The raw `ui-monospace, monospace` stack is gone (it bought no scaling in the fixed DS title).
    expect(body).not.toContain("ui-monospace");
  });
});

describe("DS type-scale 2a — IdentityPanel soul textarea", () => {
  it("uses the DS sm step, not the 13px literal", () => {
    expect(identityPanelTsx.length, "IdentityPanel.tsx imported empty").toBeGreaterThan(100);
    expect(identityPanelTsx).toContain('fontSize: "var(--pl-font-size-sm)"');
    expect(identityPanelTsx).not.toContain('fontSize: "13px"');
    // The mono face is untouched by this card.
    expect(identityPanelTsx).toContain('fontFamily: "var(--pl-font-mono)"');
  });
});

describe("DS type-scale 2a — the guard patterns still bite (meta-guard, literals built by concat)", () => {
  it("rem/em pattern matches rem and em but not a bare token", () => {
    expect(REM_EM_FONT_SIZE.test("font-size: " + "0.8rem;")).toBe(true);
    expect(REM_EM_FONT_SIZE.test("font-size: " + "0.85em;")).toBe(true);
    expect(REM_EM_FONT_SIZE.test("font-size: var(--pl-font-size-sm);")).toBe(false);
  });

  it("bare/fallback token patterns discriminate", () => {
    expect(BARE_TOKEN.test("var(--pl-font-size-xs)")).toBe(true);
    expect(TOKEN_WITH_FALLBACK.test("var(--pl-font-size-sm" + ", 13px)")).toBe(true);
    expect(TOKEN_WITH_FALLBACK.test("var(--pl-font-size-sm)")).toBe(false);
  });
});
