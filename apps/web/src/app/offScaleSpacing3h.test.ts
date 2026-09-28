import { describe, expect, it } from "vitest";

// DS audit (rule off-scale-length), spacing card 3h: the regression guard that keeps the
// four surfaces this card tokenized on the DS spacing scale. The DS ships a fixed spacing
// scale — `--pl-space-{1,2,3,4,6}` (4/8/12/16/24px) — and this card moved every EXACT-scale
// px value in a spacing declaration (padding*, margin*, gap, row-gap, column-gap) onto those
// tokens. A raw exact-scale px that comes back is a site that no longer tracks the operator's
// chosen density (the tokens can be rescaled at the DS root; a hardcoded px cannot). Off-scale
// half-steps (2/3/5/6/7/9/10/34px, …) are intentionally LEFT as literals — they wait on the DS
// gap protoContent#547 — so this guard flags ONLY the five exact-scale values, and only when
// they sit in a spacing declaration (a `left: 8px` / `top: 8px` is not spacing).
//
// The one deliberate carve-out is the mobile shell's home-indicator gutter
// `padding-bottom: max(env(safe-area-inset-bottom), 12px)` (mobile-shell.css): its 12px is a
// hardware safe-area fallback pinned verbatim by mobileBottomInset.test.ts, not a density token
// candidate. `offendersIn` skips any spacing value that reads an `env(safe-area-inset*)`.
//
// Vite `?raw` globs rather than node:fs, for the same reason as fontSizeGuard.test.ts /
// tokenNameGuard.test.ts: this tsconfig has no node types and under jsdom `import.meta.url`
// is an http: URL, so URL-relative filesystem access is a trap. The glob is compile-time,
// rooted at this file in src/app; the four owned files live in sibling directories (and one in
// src/app itself), so it reaches out with `../**/*.css`. This guard is a `.ts` and the glob only
// matches `.css`, so it is never swept; its example literals are assembled by concat below so no
// bare exact-scale spacing literal lives here to self-flag. Every `var(--pl-space-N)` it spells is
// a COMPLETE, DS-defined token, so tokenNameGuard's tree sweep clears it (a bare `var(--pl-space-`
// prefix built by concat would report a phantom `--pl-space-` — the trap this file avoids).
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// The four files this card owns, keyed by their importer-relative glob key. Three are siblings of
// this test's directory (`../<dir>/name`); mobile-shell.css sits alongside this test in src/app,
// so vite normalizes its key to `./mobile-shell.css` (same-directory), not `../app/…`.
const OWNED = [
  "./mobile-shell.css",
  "../chat/tool-calls.css",
  "../settings/delegates.css",
  "../watches/watches.css",
];

// Report path: turn the importer-relative glob key into a repo-relative `src/<dir>/<name>`.
// `../` climbs to src; `./` is this test's own src/app directory.
const pretty = (file: string): string =>
  file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");

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
// after the digits clears `4.5px`. Half-steps (2/5/6/7/9/10/34px) never match — they are not
// in the alternation. Built from a string so this file has no bare `4px`/`8px`/… of its own.
const SCALE_PX_SRC = "(?<![\\w.-])(4|8|12|16|24)px\\b";

// A device-safe-area gutter (`max(env(safe-area-inset-bottom), 12px)`): a hardware fallback,
// not a density-scale value, and pinned verbatim by mobileBottomInset.test.ts. Any spacing
// value that reads an env(safe-area-inset*) is exempt from the exact-scale sweep.
const SAFE_AREA_INSET = /env\(\s*safe-area-inset/;

// Every exact-scale spacing hit on one line, as { decl, value }. `decl` is the whole
// `prop: value` (for the report), `value` the offending `<n>px` (for meta-assertions).
// This is the RAW matcher — it does NOT apply the safe-area carve-out (that lives in
// `offendersIn`), so the meta-guard below can prove the matcher still SEES the pinned inset.
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
// The pinned safe-area gutter is skipped here (see SAFE_AREA_INSET).
function offendersIn(file: string, raw: string): string[] {
  const hits: string[] = [];
  stripComments(raw)
    .split("\n")
    .forEach((lineText, i) => {
      for (const { decl } of spacingHits(lineText)) {
        if (SAFE_AREA_INSET.test(decl)) continue;
        hits.push(`${pretty(file)}:${i + 1} ${decl}`);
      }
    });
  return hits;
}

// Sorted `src/<dir>/<name>:<line> <declaration>` across the four owned stylesheets.
function sweep(): string[] {
  return OWNED.flatMap((file) => offendersIn(file, CSS_SOURCES[file] ?? "")).sort();
}

describe("no exact-scale px spacing literal in the mobile-shell/tool-calls/delegates/watches CSS (DS audit spacing 3h)", () => {
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
    // Mixed shorthand: only the exact-scale members are flagged, the half-step (34px) is not.
    expect(spacingHits("  padding: 10px 34px 10px 12" + "px;").map((h) => h.value)).toEqual(["12px"]);
    // A leading `0` member is fine; the exact-scale member is still flagged.
    expect(spacingHits("  padding: 0 4" + "px;").map((h) => h.value)).toEqual(["4px"]);
    expect(spacingHits("  margin-bottom: 8" + "px;").map((h) => h.value)).toEqual(["8px"]);
    expect(spacingHits("  margin-top: 24" + "px;").map((h) => h.value)).toEqual(["24px"]);
  });

  it("the matcher leaves half-steps, negatives, tokens, and non-spacing props alone", () => {
    // Half-steps and off-scale values wait on protoContent#547 — never flagged.
    for (const half of ["2", "3", "5", "6", "7", "9", "10", "34"]) {
      expect(spacingHits("  padding: " + half + "px;")).toEqual([]);
    }
    // Negative margins keep their sign and are not spacing to tokenize.
    expect(spacingHits("  margin: -6" + "px -2px;")).toEqual([]);
    // A larger number that merely ends in an exact-scale digit run is not a hit.
    expect(spacingHits("  padding: 114" + "px;")).toEqual([]);
    // Already-tokenized values have no px to match.
    expect(spacingHits("  gap: " + "var(--pl-space-2);")).toEqual([]);
    // Non-spacing properties keep their px literals — positioning / radius / size are out of scope.
    expect(spacingHits("  left: 8" + "px;")).toEqual([]);
    expect(spacingHits("  top: 8" + "px;")).toEqual([]);
    expect(spacingHits("  border-radius: 4" + "px;")).toEqual([]);
    expect(spacingHits("  width: 16" + "px;")).toEqual([]);
  });

  it("reports src/<dir>/<name>:<line> <declaration> for a real offender (meta-guard, by concat)", () => {
    const bad = ".x {\n  padding: 8" + "px 10px;\n}";
    expect(offendersIn("../chat/tool-calls.css", bad)).toEqual([
      "src/chat/tool-calls.css:2 padding: 8px 10px",
    ]);
    // A px inside a comment is stripped before the sweep, so it is not a hit.
    const commented = "/* padding: 12" + "px (legacy) */\n.x { padding: " + "var(--pl-space-3); }";
    expect(offendersIn("../watches/watches.css", commented)).toEqual([]);
  });

  it("carves out the pinned safe-area gutter — the raw matcher sees its 12px, the sweep clears it", () => {
    // mobile-shell.css keeps `padding-bottom: max(env(safe-area-inset-bottom), 12px)` verbatim
    // (mobileBottomInset.test.ts pins it). The RAW matcher still flags the 12px…
    const inset = "  padding-bottom: max(env(safe-area-inset-bottom), 12" + "px);";
    expect(spacingHits(inset).map((h) => h.value)).toEqual(["12px"]);
    // …but offendersIn skips any env(safe-area-inset*) value, so it never reaches the sweep.
    expect(offendersIn("./mobile-shell.css", ".x {\n" + inset + "\n}")).toEqual([]);
    // And the pinned declaration is genuinely still in the file (not accidentally tokenized).
    expect(CSS_SOURCES["./mobile-shell.css"]).toContain(
      "padding-bottom: max(env(safe-area-inset-bottom), 12px)",
    );
  });

  it("proves this card's exact-scale sites now read DS tokens", () => {
    // r1: one migrated site per owned file, to prove the tokenization actually happened (a green
    // sweep alone could also mean "file emptied" — the emptiness guard above covers that half).
    expect(CSS_SOURCES["./mobile-shell.css"]).toContain("padding: 0 var(--pl-space-1)");
    expect(CSS_SOURCES["./mobile-shell.css"]).toContain("margin-bottom: var(--pl-space-2)");
    expect(CSS_SOURCES["../chat/tool-calls.css"]).toContain("gap: var(--pl-space-1)");
    expect(CSS_SOURCES["../settings/delegates.css"]).toContain("gap: var(--pl-space-3)");
    expect(CSS_SOURCES["../settings/delegates.css"]).toContain("margin-top: var(--pl-space-1)");
    expect(CSS_SOURCES["../watches/watches.css"]).toContain("gap: var(--pl-space-2)");
  });

  it("proves the half-steps this card preserved are still present as literals", () => {
    // r2 (half-steps unchanged): the first two are mixed-shorthand values where the exact-scale
    // member tokenized and the off-scale member survived — one assertion covers both invariants.
    expect(CSS_SOURCES["../watches/watches.css"]).toContain("padding: 10px 34px 10px var(--pl-space-3)");
    expect(CSS_SOURCES["../chat/tool-calls.css"]).toContain("padding: 6px var(--pl-space-2)");
    // Standalone half-steps elsewhere in the owned files are untouched.
    expect(CSS_SOURCES["./mobile-shell.css"]).toContain("gap: 9px");
    expect(CSS_SOURCES["../settings/delegates.css"]).toContain("gap: 6px");
    expect(CSS_SOURCES["../chat/tool-calls.css"]).toContain("gap: 10px");
  });
});
