import { describe, expect, it } from "vitest";

// DS audit (rule off-scale-length), spacing card 3b: the regression guard that keeps the
// three surfaces this card tokenized on the DS spacing scale. The DS ships a fixed spacing
// scale — `--pl-space-{1,2,3,4,6}` (4/8/12/16/24px) — and this card moved every EXACT-scale
// px value in a spacing declaration (padding*, margin*, gap, row-gap, column-gap) onto those
// tokens. A raw exact-scale px that comes back is a site that no longer tracks the operator's
// chosen density (the tokens can be rescaled at the DS root; a hardcoded px cannot). Off-scale
// half-steps (2/3/5/6/7/10/14/18px, …) got their own DS tokens in protoContent#547 (2→0_5, 6→1_5,
// 10→2_5, …) and the step-3 cards migrate them per-file; this guard still flags ONLY the five
// exact-scale values, and only when they sit in a spacing declaration (a `left: 8px` /
// `border-radius: 4px` is not spacing).
//
// Vite `?raw` globs rather than node:fs, for the same reason as fontSizeGuard.test.ts /
// tokenNameGuard.test.ts: this tsconfig has no node types and under jsdom `import.meta.url`
// is an http: URL, so URL-relative filesystem access is a trap. The glob is compile-time,
// rooted at this file in src/app; the three owned files live in sibling directories, so it
// reaches out with `../**/*.css`. This guard is a `.ts` and the glob only matches `.css`, so
// it is never swept; its example literals are assembled by concat below so no bare exact-scale
// spacing literal lives here to self-flag. Every `var(--pl-space-N)` it spells is a COMPLETE,
// DS-defined token, so tokenNameGuard's tree sweep clears it (a bare `var(--pl-space-` prefix
// built by concat would report a phantom `--pl-space-` — the trap this file avoids).
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// The three files this card owns. Keyed as `../<dir>/name` (siblings of this test in src/app).
// (providers.css is a sibling card, 3b2.)
const OWNED = [
  "../workflows/workflows.css",
  "../settings/plugins.css",
  "../settings/pathpicker.css",
];

// Report path: turn the importer-relative glob key into a repo-relative `src/<dir>/<name>`.
const pretty = (file: string): string => file.replace(/^\.\.\//, "src/");

// A spacing property: padding/margin (+ their physical & logical longhands) and the gaps.
// NOT `left`/`right`/`top`/`bottom`/`inset`/`border-radius` — those keep their px literals.
// Assembled as a RegExp *string* so this file holds no live CSS of its own.
const SPACING_PROP =
  "(?:padding|margin)(?:-(?:top|right|bottom|left|inline|block|start|end))?|row-gap|column-gap|gap";
// A declaration: the property in property position (`… : value`), value up to `;`/`}`/`{`.
// The leading boundary keeps `-webkit-margin-*` and value-position `padding-box` from matching.
const SPACING_DECL_SRC = "(?:^|[;{}\\s])(" + SPACING_PROP + ")\\s*:\\s*([^;{}]*)";
// An EXACT-scale px length inside a value: 4/8/12/16/24 only. The lookbehind clears negatives
// (`-4px`), larger numbers (`14px`, `114px`), and decimals (`1.4px`); requiring `px` right
// after the digits clears `4.5px`. Half-steps (2/3/5/6/7/10/14/18px) never match — they are not
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

// Sorted `src/<dir>/<name>:<line> <declaration>` across the three owned stylesheets.
function sweep(): string[] {
  return OWNED.flatMap((file) => offendersIn(file, CSS_SOURCES[file] ?? "")).sort();
}

describe("no exact-scale px spacing literal in the workflows/plugins/pathpicker CSS (DS audit spacing 3b)", () => {
  it("sweeps the three files: every 4/8/12/16/24px spacing value reads a DS token", () => {
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
    // Mixed shorthand: only the exact-scale members are flagged, the half-step (10px) is not.
    expect(spacingHits("  padding: 8" + "px 10px;").map((h) => h.value)).toEqual(["8px"]);
    expect(spacingHits("  margin-top: 24" + "px;").map((h) => h.value)).toEqual(["24px"]);
    expect(spacingHits("  padding-left: 4" + "px;").map((h) => h.value)).toEqual(["4px"]);
    // A two-value gap (row-gap column-gap) reports each exact-scale member.
    expect(spacingHits("  gap: 4" + "px 12px;").map((h) => h.value)).toEqual(["4px", "12px"]);
  });

  it("the matcher leaves half-steps, negatives, tokens, and non-spacing props alone", () => {
    // Half-steps and off-scale values wait on protoContent#547 — never flagged.
    for (const half of ["2", "3", "5", "6", "7", "10", "14", "18"]) {
      expect(spacingHits("  padding: " + half + "px;")).toEqual([]);
    }
    // Negative margins keep their sign and are not spacing to tokenize.
    expect(spacingHits("  margin: -6" + "px -2px;")).toEqual([]);
    // A larger number that merely ends in an exact-scale digit run is not a hit.
    expect(spacingHits("  padding: 114" + "px;")).toEqual([]);
    // Already-tokenized values have no px to match.
    expect(spacingHits("  gap: " + "var(--pl-space-2);")).toEqual([]);
    // Non-spacing properties keep their px literals — positioning / radius / sizing are out of scope.
    expect(spacingHits("  left: 8" + "px;")).toEqual([]);
    expect(spacingHits("  border-radius: 4" + "px;")).toEqual([]);
    expect(spacingHits("  width: 16" + "px;")).toEqual([]);
  });

  it("reports src/<dir>/<name>:<line> <declaration> for a real offender (meta-guard, by concat)", () => {
    const bad = ".x {\n  padding: 8" + "px 10px;\n}";
    expect(offendersIn("../workflows/workflows.css", bad)).toEqual([
      "src/workflows/workflows.css:2 padding: 8px 10px",
    ]);
    // A px inside a comment is stripped before the sweep, so it is not a hit.
    const commented = "/* padding: 12" + "px (legacy) */\n.x { padding: " + "var(--pl-space-3); }";
    expect(offendersIn("../settings/plugins.css", commented)).toEqual([]);
  });

  it("pins the workflows/plugins/pathpicker half-steps now tokenized off protoContent#547", () => {
    // protoContent#547 shipped the DS spacing half-steps in @protolabsai/design 0.11.0, so the
    // radius+spacing step-3 cards moved these surfaces' off-scale spacing onto --pl-space-{0_5,1_5,2_5}.
    // These were mixed-shorthand values whose half-step member is now a token too; re-pinned to the
    // fully tokenized strings so a regression that reintroduces a raw 6px/10px here is caught.
    expect(CSS_SOURCES["../workflows/workflows.css"]).toContain(
      "padding: var(--pl-space-1_5) var(--pl-space-2)",
    );
    expect(CSS_SOURCES["../workflows/workflows.css"]).toContain(
      "padding: var(--pl-space-2) var(--pl-space-2_5)",
    );
    // plugins.css: the marketplace-link padding (was `10px var(--pl-space-3)`) and pathpicker.css:
    // the browser-row padding (was `6px var(--pl-space-2)`) are tokenized by THIS card.
    expect(CSS_SOURCES["../settings/plugins.css"]).toContain(
      "padding: var(--pl-space-2_5) var(--pl-space-3)",
    );
    expect(CSS_SOURCES["../settings/pathpicker.css"]).toContain(
      "padding: var(--pl-space-1_5) var(--pl-space-2)",
    );
  });
});
