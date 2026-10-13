import { describe, expect, it } from "vitest";

// DS audit (rule off-scale-length), spacing card 3a — step 3 of protoContent#525/#547: the
// regression guard that keeps the two surfaces this card tokenized on the DS spacing + radius
// scales. The DS spacing scale is now the fuller `--pl-space-{0_5,1,1_5,2,2_5,3,4,5,6,8,12}`
// (2/4/6/8/10/12/16/20/24/32/48px): the half-steps shipped in @protolabsai/design 0.11.0 when
// protoContent#547 landed, so this step tokenized BOTH the exact-scale values AND the half-steps
// (2/6/10px exact; 3/5/7/9/14px snapped to their nearest step) in every spacing declaration
// (padding*, margin*, gap, row-gap, column-gap), and migrated every radius px onto the #525
// radius scale (--pl-radius / -md / -lg / -pill). A raw scale-or-half-step px that comes back is
// a site that no longer tracks the operator's chosen density (tokens can be rescaled at the DS
// root; a hardcoded px cannot). The sweep below still flags ONLY the five EXACT-scale values
// (4/8/12/16/24px) in a spacing declaration (a `left: 8px` / `border-radius: 4px` is not
// spacing) — that is the drift class it was built for; the half-step migration this step
// performed is asserted by the r2 block + the dedicated half-step scan, and the radius migration
// by its own scan. Off-grid survivors (18/22/28/33/60px) have no DS token and stay literal.
//
// Two deliberate carve-outs, both in theme.css, and both covered by the same rule: a spacing
// value that reads an `env(safe-area-inset*)` is exempt. One is the auth-overlay / model-sheet's
// home-indicator gutter `padding-bottom: max(env(safe-area-inset-bottom), 12px)` — a hardware
// fallback pinned verbatim by mobileBottomInset.test.ts, not a density-token candidate. The other
// is the composer model sheet's `padding: … calc(8px + env(safe-area-inset-bottom))` — the two
// bare exact-scale members tokenized, but the calc() interior is left alone (calc interiors are
// out of scope). Skipping any env(safe-area-inset*) value clears both.
//
// Vite `?raw` globs rather than node:fs, for the same reason as fontSizeGuard.test.ts /
// tokenNameGuard.test.ts: this tsconfig has no node types and under jsdom `import.meta.url`
// is an http: URL, so URL-relative filesystem access is a trap. The glob is compile-time,
// rooted at this file in src/app; theme.css sits alongside this test in src/app (vite keys it
// `./theme.css`), docviewer.css is a sibling directory (`../docviewer/…`). This guard is a `.ts`
// and the glob only matches `.css`, so it is never swept; its example literals are assembled by
// concat below so no bare exact-scale spacing literal lives here to self-flag. Every
// `var(--pl-space-N)` it spells is a COMPLETE, DS-defined token, so tokenNameGuard's tree sweep
// clears it (a bare `var(--pl-space-` prefix built by concat would report a phantom `--pl-space-`).
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// The two files this card owns, keyed by their importer-relative glob key. theme.css sits
// alongside this test in src/app, so vite normalizes its key to `./theme.css` (same-directory),
// not `../app/…`; docviewer.css lives in a sibling directory (`../docviewer/…`).
const OWNED = ["./theme.css", "../docviewer/docviewer.css"];

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
// after the digits clears `4.5px`. This matcher stays scoped to the exact-scale five — the
// half-steps (2/6/10/14px) this step also migrated are checked by the dedicated half-step scan
// further down, and off-grid survivors (22/28/33px) have no DS token, so neither class is in
// this alternation. Built from a string so this file has no bare `4px`/`8px`/… of its own.
const SCALE_PX_SRC = "(?<![\\w.-])(4|8|12|16|24)px\\b";

// A device-safe-area value (`max(env(safe-area-inset-bottom), 12px)`, `calc(8px + env(…))`): the
// hardware fallback is pinned verbatim by mobileBottomInset.test.ts, and a calc() interior beside
// env() is out of scope. Any spacing value that reads an env(safe-area-inset*) is exempt.
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
// The pinned safe-area gutter / calc-interior beside env() are skipped here (see SAFE_AREA_INSET).
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

// Sorted `src/<dir>/<name>:<line> <declaration>` across the two owned stylesheets.
function sweep(): string[] {
  return OWNED.flatMap((file) => offendersIn(file, CSS_SOURCES[file] ?? "")).sort();
}

describe("no exact-scale px spacing literal in the app/theme + docviewer CSS (DS audit spacing 3a)", () => {
  it("sweeps the two files: every 4/8/12/16/24px spacing value reads a DS token", () => {
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
    // Mixed shorthand: only the exact-scale members are flagged, the half-step (33px) is not.
    expect(spacingHits("  padding: 4px 12px 12px 33" + "px;").map((h) => h.value)).toEqual([
      "4px",
      "12px",
      "12px",
    ]);
    expect(spacingHits("  margin-top: 24" + "px;").map((h) => h.value)).toEqual(["24px"]);
    expect(spacingHits("  column-gap: 8" + "px;").map((h) => h.value)).toEqual(["8px"]);
  });

  it("the matcher leaves half-steps, negatives, tokens, and non-spacing props alone", () => {
    // The exact-scale sweep matcher stays scoped to 4/8/12/16/24, so half-steps (now migrated
    // by this step) and off-grid survivors alike are never flagged by IT — the half-step
    // migration is covered by the dedicated half-step scan below.
    for (const half of ["2", "6", "10", "14", "22", "28", "33"]) {
      expect(spacingHits("  padding: " + half + "px;")).toEqual([]);
    }
    // Negative margins keep their sign and are not spacing to tokenize.
    expect(spacingHits("  margin: -6" + "px 0;")).toEqual([]);
    // A larger number that merely ends in an exact-scale digit run is not a hit.
    expect(spacingHits("  padding: 114" + "px;")).toEqual([]);
    // Already-tokenized values have no px to match.
    expect(spacingHits("  gap: " + "var(--pl-space-2);")).toEqual([]);
    // Non-spacing properties keep their px literals — positioning / radius / sizing are out of scope.
    expect(spacingHits("  right: 8" + "px;")).toEqual([]);
    expect(spacingHits("  border-radius: 4" + "px;")).toEqual([]);
    expect(spacingHits("  width: 16" + "px;")).toEqual([]);
  });

  it("reports src/<dir>/<name>:<line> <declaration> for a real offender (meta-guard, by concat)", () => {
    const bad = ".x {\n  padding: 8" + "px 10px;\n}";
    expect(offendersIn("./theme.css", bad)).toEqual(["src/app/theme.css:2 padding: 8px 10px"]);
    // A px inside a comment is stripped before the sweep, so it is not a hit.
    const commented = "/* padding: 12" + "px (legacy) */\n.x { padding: " + "var(--pl-space-3); }";
    expect(offendersIn("../docviewer/docviewer.css", commented)).toEqual([]);
  });

  it("carves out the pinned safe-area gutter + the calc-beside-env member (raw matcher still sees them)", () => {
    // theme.css keeps `padding-bottom: max(env(safe-area-inset-bottom), 12px)` verbatim
    // (mobileBottomInset.test.ts pins it). The RAW matcher still flags the 12px…
    const inset = "  padding-bottom: max(env(safe-area-inset-bottom), 12" + "px);";
    expect(spacingHits(inset).map((h) => h.value)).toEqual(["12px"]);
    // …but offendersIn skips any env(safe-area-inset*) value, so it never reaches the sweep.
    expect(offendersIn("./theme.css", ".x {\n" + inset + "\n}")).toEqual([]);
    // The composer model sheet's calc()-interior member likewise reads env() and is exempt.
    const sheet = "  padding: var(--pl-space-2) var(--pl-space-2) calc(8" + "px + env(safe-area-inset-bottom));";
    expect(offendersIn("./theme.css", ".x {\n" + sheet + "\n}")).toEqual([]);
    // And both pinned declarations are genuinely still in the file (not accidentally tokenized).
    expect(CSS_SOURCES["./theme.css"]).toContain(
      "padding-bottom: max(env(safe-area-inset-bottom), 12px)",
    );
    expect(CSS_SOURCES["./theme.css"]).toContain(
      "padding: var(--pl-space-2) var(--pl-space-2) calc(8px + env(safe-area-inset-bottom))",
    );
  });

  it("r1 — proves this card's exact-scale sites now read DS tokens", () => {
    // One migrated site per owned file (plus the 24px→space-6 and the shorthand cases), to prove
    // the tokenization actually happened (a green sweep alone could also mean "file emptied" — the
    // emptiness guard above covers that half).
    expect(CSS_SOURCES["./theme.css"]).toContain("padding: 0 var(--pl-space-1) var(--pl-space-2)");
    expect(CSS_SOURCES["./theme.css"]).toContain("gap: var(--pl-space-1) var(--pl-space-2)");
    expect(CSS_SOURCES["./theme.css"]).toContain("padding: var(--pl-space-6)");
    expect(CSS_SOURCES["../docviewer/docviewer.css"]).toContain("gap: var(--pl-space-2)");
    expect(CSS_SOURCES["../docviewer/docviewer.css"]).toContain("padding: var(--pl-space-2) 0");
  });

  it("r2 — proves this step tokenized the half-steps (protoContent#547 landed), off-grid survivors stay literal", () => {
    // The mixed-shorthand rows keep their off-grid siblings (33/28/22px have no DS token, so they
    // stay literal) beside their already-tokenized members — one assertion per row.
    expect(CSS_SOURCES["./theme.css"]).toContain(
      "padding: var(--pl-space-1) var(--pl-space-3) var(--pl-space-3) 33px",
    );
    expect(CSS_SOURCES["./theme.css"]).toContain("padding: 28px var(--pl-space-4)");
    expect(CSS_SOURCES["./theme.css"]).toContain("margin: 22px 0 var(--pl-space-2)");
    // The 14px this test used to pin now snaps to the --pl-space-3 step (the .playbook-card /
    // .knowledge-ingest-drop paddings both collapse to a uniform var(--pl-space-3) pair).
    expect(CSS_SOURCES["./theme.css"]).toContain("padding: var(--pl-space-3) var(--pl-space-3)");
    expect(CSS_SOURCES["./theme.css"]).not.toContain("padding: var(--pl-space-3) 14px");
    // Representative half-step migrations: 6px→1_5, 10px→2_5, and a negative −2px/−4px→calc().
    expect(CSS_SOURCES["./theme.css"]).toContain("gap: var(--pl-space-1_5)");
    expect(CSS_SOURCES["./theme.css"]).toContain("padding: var(--pl-space-2_5)");
    expect(CSS_SOURCES["./theme.css"]).toContain(
      "margin: calc(-1 * var(--pl-space-0_5)) calc(-1 * var(--pl-space-1))",
    );
    // The docviewer standalone half-step (was `gap: 2px`) is now the 0_5 token.
    expect(CSS_SOURCES["../docviewer/docviewer.css"]).toContain("gap: var(--pl-space-0_5)");
    expect(CSS_SOURCES["../docviewer/docviewer.css"]).not.toContain("gap: 2px");
  });

  it("r3 — proves the streamdown menu shadow reads a DS token, and the auth scrim stays a theme-independent near-black", () => {
    // The streamdown table "copy as" menu shadow reads the popover-shadow token (was a raw
    // `0 6px 20px rgb(…)`); assert it in its own block, right after the radius line.
    expect(CSS_SOURCES["./theme.css"]).toContain(
      "border-radius: var(--pl-radius);\n  box-shadow: var(--pl-shadow-popover);",
    );
    // The auth-dialog overlay scrim is DELIBERATELY NOT tokenized: `--pl-color-bg` is theme
    // dependent (light in light mode), which would defeat the comment's stated "fully opaque
    // near-black … so even bright content behind it can't bleed through" intent. The AuthGate is
    // a blocking 401 modal; the scrim must stay a theme-independent near-black regardless of theme.
    expect(CSS_SOURCES["./theme.css"]).toContain(
      ".pl-overlay:has(.auth-dialog) {\n  background: rgb(8, 8, 12);\n}",
    );
    // …and it must NOT read the theme bg token (guards against a re-drift back to the rejected form).
    expect(CSS_SOURCES["./theme.css"]).not.toContain(
      ".pl-overlay:has(.auth-dialog) {\n  background: var(--pl-color-bg);\n}",
    );
  });

  it("no surviving half-step px (2/3/5/6/7/9/10/14) on a spacing prop — protoContent#547 landed", () => {
    // The complement of the exact-scale sweep: once #547 shipped the half-step tokens, a raw
    // 2/3/5/6/7/9/10/14px in a spacing declaration is drift just like an exact-scale one. This
    // scan is independent of the exact-scale matcher above (which stays scoped to 4/8/12/16/24)
    // and asserts this step left none behind. Built from a string so this file holds no bare
    // half-step px of its own; env(safe-area-inset*) values are exempt, same as the sweep.
    const HALF_PX = "(?<![\\w.-])(2|3|5|6|7|9|10|14)px\\b";
    const hits: string[] = [];
    for (const file of OWNED) {
      stripComments(CSS_SOURCES[file] ?? "")
        .split("\n")
        .forEach((lineText, i) => {
          for (const m of lineText.matchAll(new RegExp(SPACING_DECL_SRC, "gi"))) {
            const decl = `${m[1]}: ${m[2].trim()}`;
            if (SAFE_AREA_INSET.test(decl)) continue;
            if (new RegExp(HALF_PX).test(m[2])) hits.push(`${pretty(file)}:${i + 1} ${decl}`);
          }
        });
    }
    expect(hits).toEqual([]);
  });

  it("radius — every border-radius px literal now reads a DS radius token (protoContent#525)", () => {
    // The radius half of this step: after protoContent#525 shipped the radius scale (--pl-radius
    // / -md / -lg / -xl / -pill in @protolabsai/design 0.11.0), no border-radius (or per-corner
    // longhand) should carry a raw px. Mirrors the acceptance grep; comments are stripped first.
    const RADIUS_PX = /radius:\s*[^;{}]*\b\d+px/;
    const hits: string[] = [];
    for (const file of OWNED) {
      stripComments(CSS_SOURCES[file] ?? "")
        .split("\n")
        .forEach((lineText, i) => {
          if (RADIUS_PX.test(lineText)) hits.push(`${pretty(file)}:${i + 1} ${lineText.trim()}`);
        });
    }
    expect(hits).toEqual([]);
    // …and the migrated radii read the right tokens: 6px→md, 8/9px→lg, 999px→pill.
    expect(CSS_SOURCES["./theme.css"]).toContain("border-radius: var(--pl-radius-md)");
    expect(CSS_SOURCES["./theme.css"]).toContain("border-radius: var(--pl-radius-lg)");
    expect(CSS_SOURCES["./theme.css"]).toContain("border-radius: var(--pl-radius-pill)");
  });
});
