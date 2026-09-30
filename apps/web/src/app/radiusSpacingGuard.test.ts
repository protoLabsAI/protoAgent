import { describe, expect, it } from "vitest";

// DS audit (rules corner-radius + off-scale-length), step 3 FINAL of protoContent#525/#547: the
// closing, TREE-WIDE regression guard for the console radius + spacing migration. Every radius and
// spacing card moved apps/web/src CSS onto the @protolabsai/design 0.11.0 scales
// (--pl-radius{,-md,-lg,-xl,-pill} and --pl-space-*); the per-file guards (offScaleSpacing3a.test.ts
// et al.) pinned each surface as it landed. This turns that work into a single invariant across the
// whole tree: a raw radius px/rem, or an off-scale spacing px, that comes back in ANY console
// stylesheet is a site that no longer tracks the operator's chosen density/rounding (tokens can be
// rescaled at the DS root; a hardcoded literal cannot), and fails here the moment it ships.
//
// Two rules, both sweeping the whole `../**/*.css` glob:
//   Rule 1 (radius): a border-radius — or any per-corner longhand (border-{top,bottom}-{left,right}-
//     radius, border-{start,end}-{start,end}-radius) — whose value carries a px OR rem literal.
//     Allowed: var(--pl-radius*), 0, 50%, inherit (none of which contain a px/rem length).
//   Rule 2 (off-scale spacing): a 2/3/5/6/7/9/10/14px literal (incl. negatives) on a spacing
//     property (padding*, margin*, gap, row-gap, column-gap, inset*, top, right, bottom, left), in
//     shorthand or longhand. Allowed: 1px, 0, var(--pl-space-*), calc(-1 * var(--pl-space-*)). The
//     exact-scale members (4/8/12/16/24) are intentionally NOT in this set — positional offsets
//     (e.g. `right: 8px`, `top: 44px`) are pinned explicitly by the cards and stay literal, and the
//     exact-scale spacing drift class is already covered by offScaleSpacing3a's sweep.
//     Two carve-outs: (a) any value that reads env(safe-area-inset*) — a hardware fallback pinned by
//     mobileBottomInset.test.ts, not a density-token candidate; (b) a px in the FALLBACK position of
//     var(--pl-space-*, <px>) — app-crash.css carries these so the crash screen still lays out when
//     the token stylesheet fails to load (the same file tokenNameGuard exempts for its colour reads).
//
// Vite `?raw` globs rather than node:fs, for the same reason as tokenNameGuard.test.ts /
// offScaleSpacing3a.test.ts: this tsconfig has no node types and under jsdom `import.meta.url` is
// an http: URL, so URL-relative filesystem access is a trap. The glob is compile-time, rooted at
// this file in src/app, and picks up new stylesheets automatically. This guard is a `.ts` and the
// glob only matches `.css`, so it is never swept by ITS OWN rules; and every example literal below
// is assembled by concat (the number split from `px`/`rem`, `var(` split from `--pl-` in every
// RegExp string) so no offending literal — and no bare `var(--pl-…)` phantom — lives in this file
// for tokenNameGuard's tree sweep (which DOES scan `.ts`) to trip on. Each var(--pl-…) it spells is
// a COMPLETE, DS-defined token.
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Turn the importer-relative glob key into a repo-relative `src/<dir>/<name>`. `../` climbs to src;
// `./` is this test's own src/app directory. Mirrors offScaleSpacing3a / tokenNameGuard.
const pretty = (file: string): string =>
  file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");

// Strip `/* … */` comments before sweeping so a literal shown in a comment (a legacy note, a
// migration TODO) is not mistaken for a live declaration. Replaced by their own newline count so
// reported line numbers stay true to the original file.
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => "\n".repeat((m.match(/\n/g) ?? []).length));
}

// ── ALLOWLIST ─────────────────────────────────────────────────────────────────────────────────
// Explicit, per-entry-commented carve-outs of `src/<path>:<line>` for anything a migration card
// justified leaving literal. It is EMPTY: every #525 radius card and #547 spacing card fully
// tokenized apps/web/src, so no shipped declaration needs one. Add an entry only as
// `"src/<path>:<line>", // <reason, citing the card/PR that justified it>` AND bump the size
// assertion in the test below, so any addition is a deliberate, reviewed decision — never a silent
// widening of the sweep.
const ALLOWLIST = new Set<string>([
  // (none)
]);

// ── Rule 1: radius ───────────────────────────────────────────────────────────────────────────
// The radius property (shorthand + every physical/logical per-corner longhand). Assembled as a
// RegExp *string* so this file holds no live CSS of its own.
const RADIUS_PROP =
  "border(?:-(?:top|bottom|start|end))?(?:-(?:left|right|start|end))?-radius";
// A declaration: the property in property position (`… : value`), value up to `;`/`}`/`{`. The
// leading boundary keeps value-position mentions and `-webkit-`-prefixed props from matching.
const RADIUS_DECL_SRC = "(?:^|[;{}\\s])(" + RADIUS_PROP + ")\\s*:\\s*([^;{}]*)";
// A px OR rem length literal anywhere in a value: `\d+px`, `\d+rem`, or a decimal like `0.5rem`.
// Every allowed radius value (var(--pl-radius*), 0, 50%, inherit) contains no such literal, so its
// mere presence in a radius value is the failure. Built from a string with the digits split from
// `px`/`rem` so this file carries no offending length of its own.
const RADIUS_LEN = "\\d*\\.?\\d+(?:" + "px|" + "rem)\\b";

// Every radius declaration on one line that carries a px/rem literal, as { decl, len }.
function radiusHits(line: string): { decl: string; len: string }[] {
  const out: { decl: string; len: string }[] = [];
  for (const m of line.matchAll(new RegExp(RADIUS_DECL_SRC, "gi"))) {
    const value = m[2];
    const lit = new RegExp(RADIUS_LEN).exec(value);
    if (lit) out.push({ decl: `${m[1]}: ${value.trim()}`, len: lit[0] });
  }
  return out;
}

// ── Rule 2: off-scale spacing ────────────────────────────────────────────────────────────────
// A spacing property: padding/margin (+ their physical & logical longhands), the gaps, and the
// positional offsets/inset (+ its logical longhands). The leading boundary in the decl matcher
// keeps `border-left` / `border-bottom-width` from matching `left` / `bottom`, and value-position
// keywords (`box-shadow: inset …`) from matching `inset` (no `:` follows). Assembled as a string.
const SPACING_PROP =
  "(?:padding|margin)(?:-(?:top|right|bottom|left|(?:inline|block)(?:-(?:start|end))?))?" +
  "|inset(?:-(?:block|inline)(?:-(?:start|end))?)?" +
  "|row-gap|column-gap|gap|top|right|bottom|left";
const SPACING_DECL_SRC = "(?:^|[;{}\\s])(" + SPACING_PROP + ")\\s*:\\s*([^;{}]*)";
// An off-scale px length: 2/3/5/6/7/9/10/14, optionally negative. The lookbehind `(?<![\d.])`
// clears larger numbers that merely END in one of these runs (`114px`, `12px`) and decimals
// (`1.4px`); the leading `-?` INCLUDES negatives (`-2px`), which this rule flags (a raw negative
// margin should read calc(-1 * var(--pl-space-*)) instead). Requiring `px` right after the digits
// and a trailing `\b` clears `2.5px` and `2px`-inside-a-word. Built from a string with the digits
// split from `px` so this file holds no bare off-scale literal of its own.
const OFF_SCALE_PX = "(?<![\\d.])-?(?:2|3|5|6|7|9|10|14)" + "px\\b";
// A device-safe-area value: the hardware fallback (`max(env(safe-area-inset-bottom), 12px)`,
// `calc(52px + env(safe-area-inset-top) + 8px)`) is pinned verbatim by mobileBottomInset.test.ts —
// any spacing value that reads env(safe-area-inset*) is exempt.
const SAFE_AREA_INSET = /env\(\s*safe-area-inset/;
// A `var(--pl-space-*, <fallback>)` read: the px in FALLBACK position is exempt (app-crash.css
// keeps `var(--pl-space-2_5, 10px)` so the crash screen still lays out if the token sheet fails to
// load). Neutralized before the off-scale scan by dropping the whole var() — its token reference
// carries no off-scale px, and any px OUTSIDE the fallback stays visible to the scan. `var(` is
// split from `--pl-` (double-escaped) so tokenNameGuard's phantom sweep never reads a `var(--pl-…)`
// here.
const SPACE_FALLBACK = "var" + "\\(\\s*--pl-space-[a-z0-9_]+\\s*,[^)]*\\)";

// Every off-scale spacing hit on one line, as { decl, value }. env(safe-area-inset*) values are
// skipped whole; var(--pl-space-*, <px>) fallbacks are scrubbed before matching.
function spacingHits(line: string): { decl: string; value: string }[] {
  const out: { decl: string; value: string }[] = [];
  for (const m of line.matchAll(new RegExp(SPACING_DECL_SRC, "gi"))) {
    const value = m[2];
    if (SAFE_AREA_INSET.test(value)) continue;
    const scrubbed = value.replace(new RegExp(SPACE_FALLBACK, "gi"), "");
    for (const s of scrubbed.matchAll(new RegExp(OFF_SCALE_PX, "g"))) {
      out.push({ decl: `${m[1]}: ${value.trim()}`, value: s[0] });
    }
  }
  return out;
}

// ── The sweeps ───────────────────────────────────────────────────────────────────────────────
// Every offending declaration in one stylesheet, as `src/<dir>/<name>:<line> <declaration>`,
// honouring the ALLOWLIST. Reused by the tree sweeps and the synthetic meta-tests so both exercise
// the identical detector.
function radiusOffenders(file: string, raw: string): string[] {
  const hits: string[] = [];
  stripComments(raw)
    .split("\n")
    .forEach((lineText, i) => {
      for (const { decl } of radiusHits(lineText)) {
        const loc = `${pretty(file)}:${i + 1}`;
        if (!ALLOWLIST.has(loc)) hits.push(`${loc} ${decl}`);
      }
    });
  return hits;
}
function spacingOffenders(file: string, raw: string): string[] {
  const hits: string[] = [];
  stripComments(raw)
    .split("\n")
    .forEach((lineText, i) => {
      for (const { decl } of spacingHits(lineText)) {
        const loc = `${pretty(file)}:${i + 1}`;
        if (!ALLOWLIST.has(loc)) hits.push(`${loc} ${decl}`);
      }
    });
  return hits;
}
const dedupeSorted = (xs: string[]): string[] => [...new Set(xs)].sort();
function radiusSweep(): string[] {
  return dedupeSorted(
    Object.entries(CSS_SOURCES).flatMap(([f, raw]) => radiusOffenders(f, raw ?? "")),
  );
}
function spacingSweep(): string[] {
  return dedupeSorted(
    Object.entries(CSS_SOURCES).flatMap(([f, raw]) => spacingOffenders(f, raw ?? "")),
  );
}

describe("radius + off-scale spacing guard over all console CSS (DS audit, protoContent#525/#547)", () => {
  it("reads real stylesheet text — a stubbed (empty) css import would blind the sweep", () => {
    // apps/web/src CSS is opted into processing by vitest.config.ts `css.include`; if that
    // regresses, every ?raw css import returns "" and the sweeps pass on nothing. Floor the tree
    // size and assert each stylesheet is non-empty.
    const cssEntries = Object.entries(CSS_SOURCES);
    expect(cssEntries.every(([f]) => f.endsWith(".css"))).toBe(true);
    expect(cssEntries.length).toBeGreaterThan(20);
    for (const [file, text] of cssEntries) {
      expect(
        (text ?? "").length,
        `${file} imported empty — widen css.include in vitest.config.ts`,
      ).toBeGreaterThan(0);
    }
  });

  it("the ALLOWLIST is explicit and empty — every addition is deliberate", () => {
    // Locked at 0: every migration card fully tokenized its surface. A new entry MUST bump this
    // number and carry a per-line reason above, so widening the sweep can't happen silently.
    expect(ALLOWLIST.size).toBe(0);
  });

  // ── Rule 1: radius ──
  it("rule 1 sweeps the tree: no border-radius (or longhand) carries a px/rem literal", () => {
    expect(radiusSweep()).toEqual([]);
  });

  it("rule 1 flags px/rem on border-radius and its per-corner longhands (built by concat)", () => {
    expect(radiusHits("  border-radius: 5" + "px;").map((h) => h.len)).toEqual(["5px"]);
    expect(radiusHits("  border-top-left-radius: 7" + "px;").map((h) => h.len)).toEqual(["7px"]);
    expect(radiusHits("  border-radius: 0.5" + "rem;").map((h) => h.len)).toEqual(["0.5rem"]);
    // A logical corner longhand is covered too.
    expect(radiusHits("  border-start-end-radius: 4" + "px;").map((h) => h.len)).toEqual(["4px"]);
  });

  it("rule 1 clears the allowed radius values (tokens, 0, 50%, inherit)", () => {
    expect(radiusHits("  border-radius: " + "var(--pl-radius-md);")).toEqual([]);
    expect(radiusHits("  border-radius: 50%;")).toEqual([]);
    expect(radiusHits("  border-radius: 0;")).toEqual([]);
    expect(radiusHits("  border-radius: inherit;")).toEqual([]);
    expect(radiusHits("  border-radius: " + "var(--pl-radius-pill);")).toEqual([]);
  });

  it("rule 1 reports src/<dir>/<name>:<line> <declaration> for a real offender (meta, by concat)", () => {
    // Reintroducing `border-radius: 5px` into ANY css file fails with its file:line.
    const bad = ".x {\n  border-radius: 5" + "px;\n}";
    expect(radiusOffenders("../chat/chat.css", bad)).toEqual([
      "src/chat/chat.css:2 border-radius: 5px",
    ]);
    // A px inside a comment is stripped before the sweep, so it is not a hit.
    const commented = "/* border-radius: 6" + "px (legacy) */\n.x { border-radius: " + "var(--pl-radius-md); }";
    expect(radiusOffenders("./theme.css", commented)).toEqual([]);
  });

  // ── Rule 2: off-scale spacing ──
  it("rule 2 sweeps the tree: no off-scale (2/3/5/6/7/9/10/14) px on a spacing property", () => {
    expect(spacingSweep()).toEqual([]);
  });

  it("rule 2 flags off-scale px — incl. negatives — on spacing props (built by concat)", () => {
    expect(spacingHits("  padding: 7" + "px;").map((h) => h.value)).toEqual(["7px"]);
    expect(spacingHits("  margin: -2" + "px 0;").map((h) => h.value)).toEqual(["-2px"]);
    expect(spacingHits("  gap: 10" + "px;").map((h) => h.value)).toEqual(["10px"]);
    // A longhand and a positional offset are in scope too.
    expect(spacingHits("  padding-inline-start: 3" + "px;").map((h) => h.value)).toEqual(["3px"]);
    expect(spacingHits("  left: -14" + "px;").map((h) => h.value)).toEqual(["-14px"]);
  });

  it("rule 2 clears allowed values, on-scale/off-grid px, tokens and calc negatives", () => {
    // 1px and 0 are allowed; the exact-scale (4/8/12/16/24) and off-grid (22/28/44) px are out of
    // THIS rule's set (positional offsets stay literal), so none is flagged.
    for (const ok of ["1", "0", "4", "8", "12", "16", "24", "22", "28", "44"]) {
      expect(spacingHits("  padding: " + ok + "px;")).toEqual([]);
    }
    // A larger number that merely ends in an off-scale run is not a hit.
    expect(spacingHits("  padding: 114" + "px;")).toEqual([]);
    // Tokens and the sanctioned negative-token calc() carry no px.
    expect(spacingHits("  gap: " + "var(--pl-space-2);")).toEqual([]);
    expect(spacingHits("  margin: calc(-1 * " + "var(--pl-space-3));")).toEqual([]);
    expect(spacingHits("  padding: 1px " + "var(--pl-space-2);")).toEqual([]);
  });

  it("rule 2 does NOT match non-scope properties (width, font-size, border-width, outline-offset)", () => {
    expect(spacingHits("  width: 14" + "px;")).toEqual([]);
    expect(spacingHits("  font-size: 14" + "px;")).toEqual([]);
    expect(spacingHits("  border-width: 2" + "px;")).toEqual([]);
    expect(spacingHits("  border-left: 2" + "px solid transparent;")).toEqual([]);
    expect(spacingHits("  border-bottom-width: 2" + "px;")).toEqual([]);
    expect(spacingHits("  outline-offset: 2" + "px;")).toEqual([]);
  });

  it("rule 2 exempts env(safe-area-inset*) values and px in space-token fallback position (raw match still sees them)", () => {
    // The raw off-scale matcher still SEES the px inside both shapes …
    const inset = "  padding-bottom: max(env(safe-area-inset-bottom), 12" + "px);";
    expect(new RegExp(OFF_SCALE_PX, "g").test("10" + "px")).toBe(true);
    // … but spacingHits skips any env(safe-area-inset*) value (12px is not off-scale here anyway,
    // but the whole declaration is carved out regardless) …
    expect(spacingHits(inset)).toEqual([]);
    // … and scrubs the px in a var(--pl-space-*, <px>) fallback (10px → exempt), while a bare
    // off-scale px OUTSIDE the fallback on the same declaration still flags.
    expect(spacingHits("  gap: " + "var(--pl-space-2_5, 10px);")).toEqual([]);
    expect(spacingHits("  padding: " + "var(--pl-space-2, 10px) 3px;").map((h) => h.value)).toEqual([
      "3px",
    ]);
  });

  it("rule 2 reports src/<dir>/<name>:<line> <declaration> for a real offender (meta, by concat)", () => {
    // Reintroducing `padding: 7px` into ANY css file fails with its file:line.
    const bad = ".x {\n  padding: 7" + "px 10px;\n}";
    expect(spacingOffenders("../settings/settings.css", bad)).toEqual([
      "src/settings/settings.css:2 padding: 7px 10px",
      "src/settings/settings.css:2 padding: 7px 10px",
    ]);
    // De-duped at the sweep level, it is a single line per declaration.
    expect(dedupeSorted(spacingOffenders("../settings/settings.css", bad))).toEqual([
      "src/settings/settings.css:2 padding: 7px 10px",
    ]);
  });

  it("app-crash.css keeps its load-bearing space-token px fallbacks and stays clean", () => {
    // The one file that deliberately carries px in fallback position (so the crash screen lays out
    // when the token sheet fails to load) — present in the tree, and clean under both rules because
    // its px live only in var(--pl-space-*, <px>) fallbacks.
    const appCrash = CSS_SOURCES["./app-crash.css"];
    expect(appCrash).toBeTruthy();
    expect(appCrash).toContain("var(--pl-space-2_5, 10px)");
    expect(appCrash).toContain("var(--pl-space-1_5, 6px)");
    expect(spacingOffenders("./app-crash.css", appCrash ?? "")).toEqual([]);
    expect(radiusOffenders("./app-crash.css", appCrash ?? "")).toEqual([]);
  });
});
