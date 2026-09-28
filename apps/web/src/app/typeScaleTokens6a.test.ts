import { describe, expect, it } from "vitest";

// #3688 part 6a: the schedule builder, activity feed and code-pane stylesheets carried bare
// `font-size: <n>px` literals. The @protolabsai/design bump ships a --pl-font-size-* type
// scale (guarded at its source by app/dsTypeScale.test.ts), so every size now reads a scale
// token with NO px fallback. This pins the migration for these three files ONLY — the wider
// sweep is incremental, and the sibling 6b card owns docviewer.css / fleet.css.
//
// Source-level (Vite ?raw), same no-node-types reason as chat-css-tokens.test.ts /
// dsFallbackDrop.test.ts. Vitest stubs CSS imports to empty modules by default; vitest.config.ts
// opts all of src's CSS into processing (`test.css.include`) so `?raw` returns the real text.
import activityCss from "../activity/activity.css?raw";
import codePaneCss from "../codeviewer/code-pane.css?raw";
import scheduleCss from "../schedule/schedule.css?raw";

// Every step this card maps onto (the 9–18px band). No site in these files lands on lg/xl, so
// they are absent by design; the set here is what a `font-size:` value is allowed to carry.
const SCALE_STEPS = ["3xs", "2xs", "xs", "sm", "base", "lg", "xl"] as const;
// The `var(` prefix is split from the token by concat so this file never holds the bare literal
// tokenNameGuard.test.ts (#3682) sweeps for: `--pl-font-size-${s}` with the `var(` glued on is
// not a real DS token name (the guard captures `--pl-font-size-`, trailing dash, and flags it),
// yet it sweeps source lines, not runtime values. The joined string is unchanged at runtime.
const ALLOWED = SCALE_STEPS.map((s) => `var(` + `--pl-font-size-${s})`);

// Token occurrences per file = the number of former px sites (schedule 13, activity 9,
// code-pane 9 — the last including the --diffs-font-size custom property).
const FILES: Record<string, { css: string; tokens: number }> = {
  "codeviewer/code-pane.css": { css: codePaneCss, tokens: 9 },
  "schedule/schedule.css": { css: scheduleCss, tokens: 13 },
  "activity/activity.css": { css: activityCss, tokens: 9 },
};

// A bare `font-size: <n>px` literal — the exact shape this card removes (built by concat so
// this test file never flags itself).
const PX_FONT_SIZE = new RegExp("font-size:\\s*[0-9.]+p" + "x");
// A `font-size:` declaration's value, EXCLUDING the `--diffs-font-size:` custom property (the
// lookbehind rejects a preceding word char / hyphen, so `--diffs-font-size` never matches).
const FONT_SIZE_DECL = /(?<![\w-])font-size:\s*([^;}]+)/g;

describe("#3688 6a: schedule / activity / code-pane font-sizes ride the --pl-font-size-* scale", () => {
  for (const [name, { css, tokens }] of Object.entries(FILES)) {
    it(`${name} is loaded as raw text (the guard is not silently blind)`, () => {
      // Guarded by vitest.config.ts `test.css.include`; if that regresses the ?raw import
      // returns "" and every sweep below would pass on nothing.
      expect(css.length, `${name} imported empty — widen test.css.include`).toBeGreaterThan(0);
    });

    it(`${name} has no bare px font-size literal left`, () => {
      const offenders = css
        .split("\n")
        .map((line, i) => ({ line, i }))
        .filter(({ line }) => PX_FONT_SIZE.test(line))
        .map(({ i }) => `${name}:${i + 1}`);
      expect(offenders, `px font-size left in ${name}`).toEqual([]);
    });

    it(`${name} routes every font-size onto a mapped scale token, no fallback`, () => {
      const values = [...css.matchAll(FONT_SIZE_DECL)].map((m) => m[1].trim());
      expect(values.length).toBeGreaterThan(0);
      for (const value of values) {
        expect(ALLOWED, `unmapped font-size value \`${value}\` in ${name}`).toContain(value);
      }
    });

    it(`${name} carries exactly ${tokens} --pl-font-size-* references`, () => {
      expect(css.match(/var\(--pl-font-size-[a-z0-9]+\)/g) ?? []).toHaveLength(tokens);
    });

    it(`${name} keeps comments free of the glued \`*\` \`/\` minifier trap`, () => {
      // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a
      // comment early and silently drops downstream rules from the minified bundle.
      expect(css).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
    });
  }
});

// Pull a single top-level rule's flat body by its exact line-start selector (same helper shape
// as chat-css-tokens.test.ts): targeted rules have no nested braces, so `[^}]*` is safe.
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
  expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
  return match![0];
}

describe("#3688 6a: each site lands on the token its old px value maps to", () => {
  // [file, selector, expected step, former px] — spot-checks locking the mapping decisions.
  const SITES: Array<[css: string, selector: string, step: string, px: string]> = [
    [codePaneCss, ".code-pane__where", "xs", "12"],
    [codePaneCss, ".code-pane__note", "sm", "13"],
    [scheduleCss, ".cal-title", "sm", "13"],
    [scheduleCss, ".cal-wd", "2xs", "11"],
    [scheduleCss, ".cal-day", "xs", "12"],
    [scheduleCss, ".hour-toggle", "2xs", "11"],
    [activityCss, ".activity-role", "3xs", "10"],
    [activityCss, ".activity-content", "base", "14"],
    [activityCss, ".activity-stimulus", "xs", "12"],
    [activityCss, ".inbox-text", "sm", "13"],
  ];

  for (const [css, selector, step, px] of SITES) {
    it(`${selector} (was ${px}px) → ` + `var(` + `--pl-font-size-${step})`, () => {
      expect(rule(css, selector)).toMatch(
        new RegExp(`font-size:\\s*var\\(--pl-font-size-${step}\\)`),
      );
    });
  }

  it("code-pane hands pierre's diff viewer 12px via --diffs-font-size: var(--pl-font-size-xs)", () => {
    // The diff view renders 12px code because --diffs-font-size forwards the xs step (12px in
    // the DS scale, pinned by dsTypeScale.test.ts) into pierre's shadow root.
    expect(codePaneCss).toMatch(/--diffs-font-size:\s*var\(--pl-font-size-xs\)/);
  });
});
