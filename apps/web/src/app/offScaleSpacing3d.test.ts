import { describe, expect, it } from "vitest";

// DS audit (rule off-scale-length), spacing card 3d: the regression guard that keeps the
// four surfaces this card tokenized on the DS spacing scale. The DS ships a fixed spacing
// scale — `--pl-space-{1,2,3,4,6}` (4/8/12/16/24px) — and this card moved every EXACT-scale
// px value in a spacing declaration (padding*, margin*, gap, row-gap, column-gap) onto those
// tokens. A raw exact-scale px that comes back is a site that no longer tracks the operator's
// chosen density (the tokens can be rescaled at the DS root; a hardcoded px cannot). Off-scale
// half-steps (2/6/10/11/14/28px, …) are NOT swept by this exact-scale guard: the fleet-room /
// fleet-activity half-steps have since moved onto the DS half-step scale under protoContent#547
// (step 3), while work.css / app-drawer.css still await the sibling #547 card — either way that
// half-step migration is pinned below, not swept here. So this guard flags ONLY the five
// exact-scale values, and only when they sit in a spacing declaration (a `left: 8px` /
// `inset: -4px` is not spacing).
//
// Vite `?raw` globs rather than node:fs, for the same reason as fontSizeGuard.test.ts /
// tokenNameGuard.test.ts: this tsconfig has no node types and under jsdom `import.meta.url`
// is an http: URL, so URL-relative filesystem access is a trap. The glob is compile-time,
// rooted at this file in src/app. This guard is a `.ts` and the glob only matches `.css`, so
// it is never swept; its example literals are assembled by concat below so no bare exact-scale
// spacing literal lives here to self-flag.
const CSS_SOURCES = import.meta.glob("./*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// The four files this card owns. Keyed as `./name` (same directory as this test).
const OWNED = [
  "./fleet-room.css",
  "./fleet-activity.css",
  "./work.css",
  "./app-drawer.css",
];

// Report path: turn the importer-relative glob key into a repo-relative `src/app/<name>`.
const pretty = (file: string): string => file.replace(/^\.\//, "src/app/");

// A spacing property: padding/margin (+ their physical & logical longhands) and the gaps.
// NOT `left`/`right`/`top`/`bottom`/`inset` (positioning) or any other property — those keep
// their px literals. Assembled as a RegExp *string* so this file holds no live CSS of its own.
const SPACING_PROP =
  "(?:padding|margin)(?:-(?:top|right|bottom|left|inline|block|start|end))?|row-gap|column-gap|gap";
// A declaration: the property in property position (`… : value`), value up to `;`/`}`/`{`.
// The leading boundary keeps `-webkit-margin-*` and value-position `padding-box` from matching.
const SPACING_DECL_SRC = "(?:^|[;{}\\s])(" + SPACING_PROP + ")\\s*:\\s*([^;{}]*)";
// An EXACT-scale px length inside a value: 4/8/12/16/24 only. The lookbehind clears negatives
// (`-4px`), larger numbers (`14px`, `114px`), and decimals (`1.4px`); requiring `px` right
// after the digits clears `4.5px`. Half-steps (2/6/10/11/14/28px) never match — they are not
// in the alternation. Built from a string so this file has no bare `4px`/`8px`/… of its own.
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

// Every offending declaration in one stylesheet, as `src/app/<name>:<line> <declaration>`.
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

// Sorted `src/app/<name>:<line> <declaration>` across the four owned stylesheets.
function sweep(): string[] {
  return OWNED.flatMap((file) => offendersIn(file, CSS_SOURCES[file] ?? "")).sort();
}

describe("no exact-scale px spacing literal in the fleet/work/drawer CSS (DS audit spacing 3d)", () => {
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
    expect(spacingHits("  padding: 8" + "px;").map((h) => h.value)).toEqual(["8px"]);
    expect(spacingHits("  gap: 16" + "px;").map((h) => h.value)).toEqual(["16px"]);
    // Mixed shorthand: only the exact-scale members are flagged, the half-step (14px) is not.
    expect(spacingHits("  padding: 12" + "px 14px 8" + "px;").map((h) => h.value)).toEqual(["12px", "8px"]);
    expect(spacingHits("  margin-top: 24" + "px;").map((h) => h.value)).toEqual(["24px"]);
  });

  it("the matcher leaves half-steps, negatives, tokens, and non-spacing props alone", () => {
    // Half-steps / off-scale values are never flagged by this exact-scale matcher — the #547
    // half-step migration is pinned separately below, not this guard's concern.
    for (const half of ["2", "6", "10", "11", "14", "28"]) {
      expect(spacingHits("  padding: " + half + "px;")).toEqual([]);
    }
    // Negative margins keep their sign and are not spacing to tokenize.
    expect(spacingHits("  margin: -8" + "px;")).toEqual([]);
    // A larger number that merely ends in an exact-scale digit run is not a hit.
    expect(spacingHits("  padding: 114" + "px;")).toEqual([]);
    // Already-tokenized values have no px to match.
    expect(spacingHits("  gap: " + "var(--pl-space-2);")).toEqual([]);
    // Non-spacing properties keep their px literals — positioning is out of scope.
    expect(spacingHits("  left: 8" + "px;")).toEqual([]);
    expect(spacingHits("  inset: -4" + "px;")).toEqual([]);
    expect(spacingHits("  width: 16" + "px;")).toEqual([]);
  });

  it("reports src/app/<name>:<line> <declaration> for a real offender (meta-guard, by concat)", () => {
    const bad = ".x {\n  padding: 8" + "px 10px;\n}";
    expect(offendersIn("./work.css", bad)).toEqual(["src/app/work.css:2 padding: 8px 10px"]);
    // A px inside a comment is stripped before the sweep, so it is not a hit.
    const commented = "/* padding: 12" + "px (legacy) */\n.x { padding: " + "var(--pl-space-3); }";
    expect(offendersIn("./work.css", commented)).toEqual([]);
  });

  it("pins the #547 half-step migration: fleet CSS tokenized, sibling files still literal", () => {
    // r2, step 3 (protoContent#547): the fleet-room / fleet-activity half-steps now read DS
    // half-step tokens — pin a representative migrated declaration in each (was `padding: 10px …`
    // in fleet-room, `padding: 20px 2px` in fleet-activity).
    expect(CSS_SOURCES["./fleet-room.css"]).toContain("padding: var(--pl-space-2_5) var(--pl-space-3)");
    expect(CSS_SOURCES["./fleet-activity.css"]).toContain("padding: var(--pl-space-5) var(--pl-space-0_5)");
    // Genuinely off-scale values (uncovered by the DS scale, e.g. 28px) stay literals by design.
    expect(CSS_SOURCES["./fleet-room.css"]).toContain("padding: 28px var(--pl-space-3)");
    // work.css / app-drawer.css belong to the sibling card; their half-steps still wait on #547.
    expect(CSS_SOURCES["./work.css"]).toContain("gap: 7px");
    expect(CSS_SOURCES["./app-drawer.css"]).toContain("padding: 9px 10px");
  });
});
