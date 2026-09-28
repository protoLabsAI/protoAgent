import { describe, expect, it } from "vitest";

// #3688 part 2 — the fleet-room / fleet-activity / app-drawer / work overview stylesheets moved
// every hard-coded px font-size onto the DS type scale (var(--pl-font-size-{3xs..xl}), no px
// fallback; the token values are pinned by app/dsTypeScale.test.ts against @protolabsai/design).
// This guard asserts on the raw stylesheet text (same source-guard pattern as
// chat/chat-css-tokens.test.ts and app/dsTypeScaleFleetDoc.test.ts) so a regression — a
// re-introduced px literal or a token that grows a stray fallback arm — fails loudly here. It
// reads only the CSS text, never the DS package's token values. Vitest opts src's CSS into
// processing (vitest.config.ts `test.css.include`), which is what lets `?raw` return the real
// text instead of "".
import appDrawerCss from "./app-drawer.css?raw";
import fleetActivityCss from "./fleet-activity.css?raw";
import fleetRoomCss from "./fleet-room.css?raw";
import workCss from "./work.css?raw";

const FILES: Record<string, string> = {
  "app/fleet-room.css": fleetRoomCss,
  "app/fleet-activity.css": fleetActivityCss,
  "app/app-drawer.css": appDrawerCss,
  "app/work.css": workCss,
};

// A bare px font-size — the exact shape this card eliminates. `font-size: 12px;`
const PX_FONT_SIZE = /font-size:\s*[0-9.]+px/;
// A --pl-font-size-* var that carries a fallback arm — the card mandates NO fallback, so
// `var(--pl-font-size-xs, 12px)` (or any fallback) must never appear.
const TOKEN_WITH_FALLBACK = /var\(\s*--pl-font-size-[a-z0-9]+\s*,/;
// A bare DS type-scale token: `var(--pl-font-size-<step>)` with NO comma-list fallback.
const BARE_TOKEN = /^var\(--pl-font-size-(?:3xs|2xs|xs|sm|base|lg|xl)\)$/;

// The full multiset of font-size declarations each file must carry AFTER the migration, in
// document order. Every entry is a mapped scale step — no px, no fallback. If a value drifts or
// a new px site sneaks in, the extracted list stops matching and this fails loudly.
const EXPECTED: Record<string, readonly string[]> = {
  // .flr__empty (13→sm), .flr__name (14→base), .flr__tag (9.5→3xs), .flr__meta (11.5→2xs),
  // .flr__mention-name (13→sm), .flr__mention-meta (11→2xs), .flr__diag-name (14→base),
  // .flr__diag-state (12.5→sm), .flr__diag-note (12→xs), .flr__diag-logs (11.5→2xs),
  // .flr__diag-taskinput (12→xs), .flr__diag-blocktext/.flr__diag-pre (12→xs),
  // .flr__diag-pre (11.5→2xs), .flr__diag-msg (12→xs), .flr__diag-errbody strong (13→sm),
  // .flr__diag-errbody span (12→xs)
  "app/fleet-room.css": [
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-base)",
    "var(--pl-font-size-3xs)",
    "var(--pl-font-size-2xs)",
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-2xs)",
    "var(--pl-font-size-base)",
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-2xs)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-2xs)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-xs)",
  ],
  // .flr-feed__empty (12→xs), .flr-feed__src (12.5→sm), .flr-feed__text (12.5→sm)
  "app/fleet-activity.css": [
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-sm)",
  ],
  // .app-drawer-label (11→2xs), .app-drawer-item (14→base), .app-drawer-foot (12→xs)
  "app/app-drawer.css": [
    "var(--pl-font-size-2xs)",
    "var(--pl-font-size-base)",
    "var(--pl-font-size-xs)",
  ],
  // .work-card-pulse (12→xs), .work-row-title (13→sm), .work-row-meta (12→xs),
  // .work-card-blank .pl-empty__desc (12→xs)
  "app/work.css": [
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-sm)",
    "var(--pl-font-size-xs)",
    "var(--pl-font-size-xs)",
  ],
};

function fontSizeValues(css: string): string[] {
  return Array.from(css.matchAll(/font-size:\s*([^;]+);/g), (m) => m[1].trim());
}

describe("#3688 part 2 — fleet-room/fleet-activity/app-drawer/work font-size on DS tokens", () => {
  for (const [name, css] of Object.entries(FILES)) {
    it(`${name} is loaded as raw text (the guard is not silently blind)`, () => {
      expect(css.length).toBeGreaterThan(0);
    });

    it(`${name} carries no bare px font-size`, () => {
      expect(css).not.toMatch(PX_FONT_SIZE);
    });

    it(`${name} carries no --pl-font-size-* fallback arm`, () => {
      expect(css).not.toMatch(TOKEN_WITH_FALLBACK);
    });

    it(`${name} every font-size is a bare --pl-font-size-* token`, () => {
      const values = fontSizeValues(css);
      expect(values.length, `expected at least one font-size in ${name}`).toBeGreaterThan(0);
      for (const value of values) {
        expect(value, `unexpected font-size \`${value}\` in ${name}`).toMatch(BARE_TOKEN);
      }
    });

    it(`${name} font-size declarations are exactly the mapped scale steps`, () => {
      expect(fontSizeValues(css)).toEqual(EXPECTED[name]);
    });

    it(`${name} keeps comments free of the glued \`*\` \`/\` minifier trap`, () => {
      // Mirror scripts/check-css-comments.mjs: a `*` `/` glued to identifier chars closes a
      // comment early and silently drops downstream rules from the minified bundle.
      expect(css).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
    });
  }
});

// The half-pixel sites that SNAPPED to the nearest step (size is theme-invariant): pin the
// direction of each snap by selector so a later re-round to the wrong step is caught.
describe("#3688 part 2 — half-pixel sites snapped to the mapped step", () => {
  // Pull one top-level rule's body by its exact line-start selector (each targeted rule is flat,
  // so `[^}]*` is a safe body matcher). Same helper as app/dsTypeScaleSites.test.ts.
  function rule(css: string, selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
    expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
    return match![0];
  }

  const SNAPS: Array<[css: string, file: string, selector: string, token: string, note: string]> = [
    [fleetRoomCss, "fleet-room.css", ".flr__tag", "3xs", "9.5px → 10px"],
    [fleetRoomCss, "fleet-room.css", ".flr__meta", "2xs", "11.5px → 11px"],
    [fleetRoomCss, "fleet-room.css", ".flr__diag-state", "sm", "12.5px → 13px"],
    [fleetRoomCss, "fleet-room.css", ".flr__diag-logs", "2xs", "11.5px → 11px"],
    [fleetActivityCss, "fleet-activity.css", ".flr-feed__src", "sm", "12.5px → 13px"],
    [fleetActivityCss, "fleet-activity.css", ".flr-feed__text", "sm", "12.5px → 13px"],
  ];

  for (const [css, file, selector, token, note] of SNAPS) {
    it(`${file} ${selector} (${note}) reads --pl-font-size-${token}`, () => {
      expect(rule(css, selector)).toMatch(
        new RegExp(`font-size:\\s*var\\(--pl-font-size-${token}\\);`),
      );
    });
  }

  it("fleet-room.css .flr__diag-pre (11.5px → 11px) reads --pl-font-size-2xs", () => {
    // `.flr__diag-pre` appears twice: grouped with .flr__diag-blocktext (12→xs) and standalone
    // (the mono override that snapped). Match the standalone rule — the one that opens with
    // font-family — so we assert the right block, not the shared one the line-start helper hits.
    expect(fleetRoomCss).toMatch(
      /\.flr__diag-pre\s*\{\s*font-family:[^}]*font-size:\s*var\(--pl-font-size-2xs\);/,
    );
  });
});
