import { describe, expect, it } from "vitest";

// DS audit (rule off-scale-length), spacing card 3g — step 3 of protoContent#525/#547: the
// regression guard that keeps the four surfaces this card family tokenized on the DS spacing +
// radius scales. The DS spacing scale is now the fuller `--pl-space-{0_5,1,1_5,2,2_5,3,4,5,6,8,12}`
// (2/4/6/8/10/12/16/20/24/32/48px): the half-steps shipped in @protolabsai/design 0.11.0 when
// protoContent#547 landed. This card (bd-qwox) tokenized BOTH the exact-scale values AND the
// half-steps (2/6/10px exact; 7px snapped to its nearest step) in every spacing declaration of
// app/tools.css + goals/goals.css, migrated their two radius px onto the #525 radius scale
// (--pl-radius / -md / -lg), and tokenized the goals clear-button top/right offsets (the card's
// spacing scope includes top/right/bottom/left/inset, which the exact-scale sweep matcher below
// does not — those migrations are pinned explicitly in r2 rather than swept). The settings/
// keybindings pins were set by the sibling card bd-m3oa, which this card follows. A raw scale-or-
// half-step px that comes back is a site that no longer tracks the operator's chosen density
// (tokens can be rescaled at the DS root; a hardcoded px cannot). The sweep below still flags
// ONLY the five EXACT-scale values (4/8/12/16/24px) in a padding/margin/gap declaration (a
// `top: 8px` / `border-radius: 4px` is not swept) across all four files — that is the drift class
// it was built for; this card's half-step migration is asserted by the r2 block + the goals/tools
// half-step scan, and the radius migration by its own scan. Uncovered survivors (18/34px) have no
// DS token and stay literal.
//
// Vite `?raw` globs rather than node:fs, for the same reason as fontSizeGuard.test.ts /
// tokenNameGuard.test.ts: this tsconfig has no node types and under jsdom `import.meta.url`
// is an http: URL, so URL-relative filesystem access is a trap. The glob is compile-time,
// rooted at this file in src/app; three owned files live in sibling directories (and one —
// tools.css — in src/app itself), so it reaches out with `../**/*.css`. This guard is a `.ts`
// and the glob only matches `.css`, so it is never swept; its example literals are assembled by
// concat below so no bare exact-scale spacing literal lives here to self-flag. Every
// `var(--pl-space-N)` it spells is a COMPLETE, DS-defined token, so tokenNameGuard's tree sweep
// clears it (a bare `var(--pl-space-` prefix built by concat would report a phantom
// `--pl-space-` — the trap that file avoids).
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// The four files this card owns, keyed by their importer-relative glob key. Three are siblings of
// this test's directory (`../<dir>/name`); tools.css sits alongside this test in src/app, so vite
// normalizes its key to `./tools.css` (same-directory), not `../app/…`.
const OWNED = [
  "./tools.css",
  "../goals/goals.css",
  "../settings/settings.css",
  "../settings/keybindings.css",
];

// The two files THIS card (bd-qwox) migrated for half-steps + radius; the acceptance greps run
// over exactly these. (settings/keybindings — also in OWNED for the shared exact-scale sweep —
// were tokenized by bd-m3oa, so their half-step/radius coverage lives in that card's guard.)
const THIS_CARD = ["./tools.css", "../goals/goals.css"];

// Report path: turn the importer-relative glob key into a repo-relative `src/<dir>/<name>`.
// `../` climbs to src; `./` is this test's own src/app directory.
const pretty = (file: string): string =>
  file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");

// A spacing property FOR THE SWEEP: padding/margin (+ their physical & logical longhands) and the
// gaps. The exact-scale sweep does NOT cover `left`/`right`/`top`/`bottom`/`inset`/`border-radius`
// — this card's goals top/right migrations are pinned in r2, and radius has its own scan below.
// Assembled as a RegExp *string* so this file holds no live CSS of its own.
const SPACING_PROP =
  "(?:padding|margin)(?:-(?:top|right|bottom|left|inline|block|start|end))?|row-gap|column-gap|gap";
// A declaration: the property in property position (`… : value`), value up to `;`/`}`/`{`.
// The leading boundary keeps `-webkit-margin-*` and value-position `padding-box` from matching.
const SPACING_DECL_SRC = "(?:^|[;{}\\s])(" + SPACING_PROP + ")\\s*:\\s*([^;{}]*)";
// An EXACT-scale px length inside a value: 4/8/12/16/24 only. The lookbehind clears negatives
// (`-4px`), larger numbers (`14px`, `114px`), and decimals (`1.4px`); requiring `px` right
// after the digits clears `4.5px`. This matcher stays scoped to the exact-scale five — the
// half-steps (2/6/10px) this card migrated are checked by the dedicated half-step scan further
// down. Built from a string so this file has no bare `4px`/`8px`/… of its own.
const SCALE_PX_SRC = "(?<![\\w.-])(4|8|12|16|24)px\\b";

// Every exact-scale spacing hit on one line, as { decl, value }. `decl` is the whole
// `prop: value` (for the report), `value` the offending `<n>px` (for meta-assertions).
function spacingHits(line: string): { decl: string; value: string }[] {
  const out: { decl: string; value: string }[] = [];
  for (const m of line.matchAll(new RegExp(SPACING_DECL_SRC, "gi"))) {
    const prop = m[1];
    const value = m[2];
    for (const s of value.matchAll(new RegExp(SCALE_PX_SRC, "g"))) {
      out.push({ decl: `${prop}: ${value.trim()}`, value: `${s[1]}px` });
    }
  }
  return out;
}

// Strip `/* … */` comments before sweeping so an exact-scale px shown in a comment (a legacy
// note, a migration TODO) is not mistaken for a live declaration. Replaced by their own
// newline count so reported line numbers stay true to the original file.
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => "\n".repeat((m.match(/\n/g) ?? []).length));
}

// Every offending declaration in one stylesheet, as `src/<dir>/<name>:<line> <declaration>`.
function offendersIn(file: string, raw: string): string[] {
  const hits: string[] = [];
  stripComments(raw)
    .split("\n")
    .forEach((lineText, i) => {
      for (const { decl } of spacingHits(lineText)) {
        hits.push(`${pretty(file)}:${i + 1} ${decl}`);
      }
    });
  return hits;
}

// Sorted `src/<dir>/<name>:<line> <declaration>` across the four owned stylesheets.
function sweep(): string[] {
  return OWNED.flatMap((file) => offendersIn(file, CSS_SOURCES[file] ?? "")).sort();
}

describe("no exact-scale px spacing literal in the goals/tools/settings/keybindings CSS (DS audit spacing 3g)", () => {
  it("sweeps the four files: every 4/8/12/16/24px spacing value reads a DS token", () => {
    expect(sweep()).toEqual([]);
  });

  it("reads real stylesheet text — a stubbed (empty) css import would blind the sweep", () => {
    // apps/web/src CSS is opted into processing by vitest.config.ts `css.include`; if that
    // regresses, every ?raw css import returns "" and the sweep passes on nothing. Assert each
    // owned stylesheet is present and non-empty.
    for (const file of OWNED) {
      expect(Object.keys(CSS_SOURCES), `${file} missing from glob`).toContain(file);
      expect((CSS_SOURCES[file] ?? "").length, `${file} imported empty`).toBeGreaterThan(0);
    }
  });

  it("the matcher flags exact-scale px only in spacing declarations, built by concat", () => {
    expect(spacingHits("  gap: 8" + "px;").map((h) => h.value)).toEqual(["8px"]);
    expect(spacingHits("  gap: 16" + "px;").map((h) => h.value)).toEqual(["16px"]);
    expect(spacingHits("  margin-top: 12" + "px;").map((h) => h.value)).toEqual(["12px"]);
    // Mixed shorthand: only the exact-scale members are flagged, the half-steps (10/34px) are not.
    expect(spacingHits("  padding: 10px 34px 10px 12" + "px;").map((h) => h.value)).toEqual(["12px"]);
    // A leading `0` member is fine; the exact-scale member is still flagged.
    expect(spacingHits("  padding: 0 4" + "px;").map((h) => h.value)).toEqual(["4px"]);
    // Three-value margin: each exact-scale member flagged, the off-scale member skipped.
    expect(spacingHits("  margin: 4px 0 12" + "px;").map((h) => h.value)).toEqual(["4px", "12px"]);
  });

  it("the matcher leaves half-steps, negatives, tokens, and non-spacing props alone", () => {
    // Half-steps and off-scale values wait on protoContent#547 — never flagged.
    for (const half of ["2", "6", "7", "10", "14", "18", "28", "34"]) {
      expect(spacingHits("  padding: " + half + "px;")).toEqual([]);
    }
    // Negative margins keep their sign and are not spacing to tokenize.
    expect(spacingHits("  margin: -6" + "px -2px;")).toEqual([]);
    // A larger number that merely ends in an exact-scale digit run is not a hit.
    expect(spacingHits("  padding: 114" + "px;")).toEqual([]);
    // Already-tokenized values have no px to match.
    expect(spacingHits("  gap: " + "var(--pl-space-2);")).toEqual([]);
    // Non-spacing properties keep their px literals — positioning / radius / size are out of scope.
    expect(spacingHits("  top: 8" + "px;")).toEqual([]);
    expect(spacingHits("  border-radius: 4" + "px;")).toEqual([]);
    expect(spacingHits("  min-width: 84" + "px;")).toEqual([]);
    expect(spacingHits("  grid-template-columns: 84" + "px 1fr;")).toEqual([]);
  });

  it("reports src/<dir>/<name>:<line> <declaration> for a real offender (meta-guard, by concat)", () => {
    const bad = ".x {\n  padding: 8" + "px 10px;\n}";
    expect(offendersIn("../goals/goals.css", bad)).toEqual([
      "src/goals/goals.css:2 padding: 8px 10px",
    ]);
    // tools.css is same-directory, so its report path resolves through src/app.
    expect(offendersIn("./tools.css", bad)).toEqual([
      "src/app/tools.css:2 padding: 8px 10px",
    ]);
    // A px inside a comment is stripped before the sweep, so it is not a hit.
    const commented = "/* padding: 12" + "px (legacy) */\n.x { padding: " + "var(--pl-space-3); }";
    expect(offendersIn("../settings/settings.css", commented)).toEqual([]);
  });

  it("proves this card's exact-scale sites now read DS tokens", () => {
    // r1: one migrated site per owned file, to prove the tokenization actually happened (a green
    // sweep alone could also mean "file emptied" — the emptiness guard above covers that half).
    expect(CSS_SOURCES["./tools.css"]).toContain("margin: var(--pl-space-1) 0 var(--pl-space-3)");
    expect(CSS_SOURCES["../goals/goals.css"]).toContain("gap: var(--pl-space-1)");
    expect(CSS_SOURCES["../goals/goals.css"]).toContain("gap: var(--pl-space-4)");
    expect(CSS_SOURCES["../settings/settings.css"]).toContain("padding: var(--pl-space-4) var(--pl-space-1)");
    expect(CSS_SOURCES["../settings/keybindings.css"]).toContain("padding: 0 var(--pl-space-1)");
  });

  it("r2 — proves this card's half-step + positional sites now read DS tokens (protoContent#547 landed)", () => {
    // protoContent#547 shipped the -0_5/-1_5/-2_5 half-step tokens (2/6/10px) in
    // @protolabsai/design 0.11.0, so this card snapped the goals/tools half-step spacing onto them.
    // The goals row padding keeps its 34px off-scale member (uncovered) between two -2_5 members;
    // tools' fs-warning padding likewise tokenizes its 10px member.
    expect(CSS_SOURCES["../goals/goals.css"]).toContain(
      "padding: var(--pl-space-2_5) 34px var(--pl-space-2_5) var(--pl-space-3)",
    );
    expect(CSS_SOURCES["./tools.css"]).toContain("padding: var(--pl-space-2) var(--pl-space-2_5)");
    // The standalone goals `gap: 6px` half-steps now read the -1_5 token.
    expect(CSS_SOURCES["../goals/goals.css"]).toContain("gap: var(--pl-space-1_5)");
    // goals' absolute-positioned clear button — the card's spacing scope includes top/right, which
    // the sweep does not cover, so pin them here: top 8px→space-2 (exact), right 6px→space-1_5.
    expect(CSS_SOURCES["../goals/goals.css"]).toContain("top: var(--pl-space-2)");
    expect(CSS_SOURCES["../goals/goals.css"]).toContain("right: var(--pl-space-1_5)");
    // (keybindings.css's `.kb-reset { padding: 2px var(--pl-space-1) }` half-step was retired with
    // the rule itself when that reset control became a DS Button — protoContent#551, card 4c.)
    // settings.css's `gap: 14px` half-steps are no longer literals: the DS gap scale landed
    // (protoContent#547) with the -1_5/-2_5 half-step tokens plus the exact -4/-6/…, so the
    // radius+spacing card (protoContent#525/#547 step 3) snapped all three `gap: 14px` sites
    // (.quick-setting-body, .settings-shell, .settings-group-actions) to var(--pl-space-4) —
    // 14→16, the settings surface's dominant --pl-space-4 rhythm (padding-left, setting-row).
    expect(CSS_SOURCES["../settings/settings.css"]).toContain("gap: var(--pl-space-4)");
    // keybindings.css's `gap: 18px` (.kb-panel) is still an uncovered off-scale value — the DS
    // gap scale has no 18px step — so it stays a literal.
    expect(CSS_SOURCES["../settings/keybindings.css"]).toContain("gap: 18px");
  });

  it("no surviving half-step px (2/3/5/6/7/9/10/14) on a spacing prop in goals/tools — protoContent#547 landed", () => {
    // The complement of the exact-scale sweep, scoped to THIS card's two files: once #547 shipped
    // the half-step tokens, a raw 2/3/5/6/7/9/10/14px in a padding/margin/gap declaration is drift
    // just like an exact-scale one. Independent of the exact-scale matcher above (which stays
    // scoped to 4/8/12/16/24). Built from a string so this file holds no bare half-step px of its
    // own; the 34px (goals row) and 18px (goals list) survivors are uncovered and never in the set.
    const HALF_PX = "(?<![\\w.-])(2|3|5|6|7|9|10|14)px\\b";
    const hits: string[] = [];
    for (const file of THIS_CARD) {
      stripComments(CSS_SOURCES[file] ?? "")
        .split("\n")
        .forEach((lineText, i) => {
          for (const m of lineText.matchAll(new RegExp(SPACING_DECL_SRC, "gi"))) {
            if (new RegExp(HALF_PX).test(m[2])) hits.push(`${pretty(file)}:${i + 1} ${m[1]}: ${m[2].trim()}`);
          }
        });
    }
    expect(hits).toEqual([]);
  });

  it("radius — every border-radius px literal in goals/tools now reads a DS radius token (protoContent#525)", () => {
    // The radius half of this card: after protoContent#525 shipped the radius scale (--pl-radius
    // / -md / -lg / -xl / -pill in @protolabsai/design 0.11.0), no border-radius (or per-corner
    // longhand) should carry a raw px. Mirrors the acceptance grep; comments are stripped first.
    const RADIUS_PX = /radius:\s*[^;{}]*\b\d+px/;
    const hits: string[] = [];
    for (const file of THIS_CARD) {
      stripComments(CSS_SOURCES[file] ?? "")
        .split("\n")
        .forEach((lineText, i) => {
          if (RADIUS_PX.test(lineText)) hits.push(`${pretty(file)}:${i + 1} ${lineText.trim()}`);
        });
    }
    expect(hits).toEqual([]);
    // tools.css's two radii: the deep-link target-pulse 6px→md, the fs-projects warning 8px→lg.
    expect(CSS_SOURCES["./tools.css"]).toContain("border-radius: var(--pl-radius-md)");
    expect(CSS_SOURCES["./tools.css"]).toContain("border-radius: var(--pl-radius-lg)");
    // goals.css keeps its already-token evidence radius and its `border-radius: 0` reset untouched.
    expect(CSS_SOURCES["../goals/goals.css"]).toContain("border-radius: var(--pl-radius)");
    expect(CSS_SOURCES["../goals/goals.css"]).toContain("border-radius: 0");
  });
});
