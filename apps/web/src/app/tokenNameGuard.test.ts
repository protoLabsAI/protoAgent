import { describe, expect, it } from "vitest";

import { PL_TOKEN_VARS } from "./PluginView";
// The DS token stylesheets, read as raw text. Under this vitest config node_modules CSS is
// stubbed to "" (only apps/web/src is in `css.include`), so at test time these currently
// contribute nothing — see DEFINED below for why the design tokens alone still cover the
// tree, and why the import is kept regardless.
import dsTokensCss from "@protolabsai/design/css/tokens?raw";
import pluginKitCss from "@protolabsai/ui/plugin-kit.css?raw";

// #3682 (part 3/3): the regression guard that closes the phantom-token class of bug. A
// `var(--pl-…)` naming a custom property the installed design system never declares can
// never resolve, so the site paints its hardcoded fallback forever — deaf to light mode and
// to operator ThemePanel overrides (the same failure #2224 fixed for the status tokens, and
// #3682 parts 1/2 fixed for a handful of surfaces). This sweeps the whole console source and
// fails if any `var(--pl-…)` references a name outside the set the DS actually defines.
//
// Vite `?raw` globs rather than node:fs: this tsconfig has no node types and under jsdom
// `import.meta.url` is an http: URL, so URL-relative filesystem access is a trap (same
// reasoning as statusTokenGuard.test.ts / phantomTokenRename.test.ts). The glob is
// compile-time, rooted at this file in src/app, and picks up new source files automatically.
const SOURCES = import.meta.glob("../**/*.{css,ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): same-directory files key as
// `./name`, files in sibling directories as `../dir/name`.
//
// The only files exempt from the sweep are the ones that assert the ABSENCE of a phantom
// name, so they legitimately mention one. Per #3682 this is a closed list of three — a new
// phantom-mentioning file must instead build its literal by concat (as the guards below do),
// never join this set.
const EXEMPT = new Set<string>([
  "./tokenNameGuard.test.ts", // this guard — its meta-tests assemble synthetic names
  "./statusTokenGuard.test.ts", // sweeps for phantom --pl-color-{info,warning,danger} (#2224)
  "../chat/__tests__/hitl-accent.test.ts", // negative assertion on --pl-color-success (#2153)
]);

// A `--pl-*` custom-property DECLARATION (`--pl-name:`), used to read what a stylesheet
// defines. It cannot match a var() read: `var(--pl-x)` / `var(--pl-x, …)` have `)` or `,`
// after the name, never the `:` this requires.
const DECL = /(--pl-[a-z0-9_-]+)\s*:/g;
const declaredIn = (text: string): string[] => [...text.matchAll(DECL)].map((m) => m[1]);

// The DEFINED set — every --pl-* the installed design system declares. Derived from the
// installed packages, never hand-copied. Four live sources, unioned:
//   1. PL_TOKEN_VARS — @protolabsai/design's tokens.json, flattened by PluginView exactly the
//      way the DS build emits tokens.css. This is the load-bearing source at test time (~85
//      names on design ^0.11.0).
//   2/3. @protolabsai/ui's plugin-kit.css and @protolabsai/design's tokens.css, parsed for
//      their `--pl-*:` declarations. Vitest stubs node_modules CSS imports to "" (the
//      `css.include` anchor deliberately covers only apps/web/src, to keep DS *component* CSS
//      stubbed), so under this harness these two currently add nothing — but the import+parse
//      is kept so the derivation genuinely reads the ui kit and stays correct if that stubbing
//      ever changes. It costs no coverage today: plugin-kit.css is generated from the same
//      @protolabsai/design tokens as tokens.json, and its one token not already in tokens.json
//      (--pl-color-reasoning-bg) is referenced by no console source.
//   4. Any `--pl-*:` custom property declared inside apps/web/src itself (currently none).
const defined = new Set<string>([
  ...PL_TOKEN_VARS,
  ...declaredIn(dsTokensCss),
  ...declaredIn(pluginKitCss),
]);
// Local declarations live in CSS custom-property rules only; scan comment-stripped CSS so a
// `--pl-…:` shown in prose (or a TS fixture like `resolved(--pl-color-bg:1)`) can't seed the set.
for (const [file, text] of Object.entries(SOURCES)) {
  if (file.endsWith(".css")) for (const name of declaredIn(stripComments(text, true))) defined.add(name);
}

// A var() read of a --pl-* property. A GLOBAL match captures the first argument of every
// var() — including nested var()s in fallback position, since each nested var() also begins
// `var(` — which is exactly "the first argument of var() plus any nested var() in fallbacks".
// Built from a RegExp *string*, with `var(` split from `--pl-` at every use site, so this
// file's own source never contains a bare `var(--pl-…)` the sweep (or a copy of it) could trip
// on — belt-and-suspenders on top of the self-exemption above.
const VAR_REF = "var\\(\\s*(--pl-[a-z0-9_-]+)";
const refsIn = (line: string): string[] =>
  [...line.matchAll(new RegExp(VAR_REF, "g"))].map((m) => m[1]);

// Strip comments before sweeping so a var() shown in a doc/prose example (dsFallbackDrop.test.ts,
// dsTokenFallbackStrip.test.ts, workflows-token-fallbacks.test.ts …) is not mistaken for a live
// reference. Comments are replaced by their own newline count so reported line numbers stay true.
function stripComments(text: string, isCss: boolean): string {
  let out = text.replace(/\/\*[\s\S]*?\*\//g, (m) => "\n".repeat((m.match(/\n/g) ?? []).length));
  if (!isCss) out = out.replace(/\/\/[^\n]*/g, ""); // JS/TS line comments (CSS has none)
  return out;
}

// Every offending reference, as `src/<path>:<line> <name>`, sorted.
function sweep(): string[] {
  const hits: string[] = [];
  for (const [file, raw] of Object.entries(SOURCES)) {
    if (EXEMPT.has(file)) continue;
    const pretty = file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");
    stripComments(raw, file.endsWith(".css")).split("\n").forEach((line, i) => {
      for (const name of refsIn(line)) {
        if (!defined.has(name)) hits.push(`${pretty}:${i + 1} ${name}`);
      }
    });
  }
  return hits.sort();
}

describe("every var(--pl-*) in the console is defined by the installed DS (#3682)", () => {
  it("derives a non-vacuous defined set from the installed packages (no hand-copied list)", () => {
    // >40 so a broken import (empty PL_TOKEN_VARS + stubbed CSS) fails loudly instead of
    // letting the sweep pass on an empty set. PL_TOKEN_VARS is the load-bearing source, so
    // floor it on its own too — the other sources are harness-stubbed and must not mask it.
    expect(defined.size).toBeGreaterThan(40);
    expect(PL_TOKEN_VARS.length).toBeGreaterThan(40);
    // Spot-check well-known names that only appear if the derivation actually ran.
    for (const n of [
      "--pl-color-fg",
      "--pl-color-accent",
      "--pl-color-status-error",
      "--pl-radius",
      "--pl-radius-md",
      "--pl-radius-pill",
      "--pl-space-0_5",
      "--pl-space-1_5",
      "--pl-space-2_5",
    ]) {
      expect(defined.has(n)).toBe(true);
    }
  });

  it("VAR_REF captures underscore token names whole (meta-guard, built by concat)", () => {
    // `var(--pl-space-0_5)` must be captured as "--pl-space-0_5", not truncated to "--pl-space-0".
    expect(refsIn("gap: " + "var(" + "--pl-space-0_5);")).toEqual(["--pl-space-0_5"]);
  });

  it("sweeps the tree: no var(--pl-…) references a token the DS does not define", () => {
    expect(sweep()).toEqual([]);
  });

  it("reads real stylesheet text — a stubbed (empty) css import would blind the sweep", () => {
    // apps/web/src CSS is opted into processing by vitest.config.ts `css.include`; if that
    // regresses, every ?raw css import returns "" and the sweep passes on nothing. Assert each
    // src stylesheet is non-empty, and floor the tree size so a glob typo can't sweep an empty set.
    const cssEntries = Object.entries(SOURCES).filter(([f]) => f.endsWith(".css"));
    for (const [file, text] of cssEntries) {
      expect(text.length, `${file} imported empty — widen css.include in vitest.config.ts`).toBeGreaterThan(0);
    }
    expect(cssEntries.length).toBeGreaterThan(20);
    expect(Object.keys(SOURCES).length).toBeGreaterThan(100);
  });

  it("flags a synthetic phantom and would report file:line (meta-guard, name built by concat)", () => {
    // A name the DS never declares, assembled so this file holds no bare `var(--pl-…)` literal.
    const phantom = "--pl-color-" + "text-muted";
    const line = "color: " + "var(" + phantom + ", #fff);";
    expect(refsIn(line)).toEqual([phantom]); // the matcher captures it …
    expect(defined.has(phantom)).toBe(false); // … and it is not in the defined set → a hit

    // Prove the file:line shape a real hit would take, without editing the tree: run the same
    // matcher+lookup over a two-line synthetic source and format exactly as sweep() does.
    const synthetic = ["ok: " + "var(" + "--pl-color-" + "fg);", "bad: " + "var(" + phantom + ");"].join("\n");
    const hits: string[] = [];
    synthetic.split("\n").forEach((l, i) => {
      for (const name of refsIn(l)) if (!defined.has(name)) hits.push(`src/synthetic.css:${i + 1} ${name}`);
    });
    expect(hits).toEqual([`src/synthetic.css:2 ${phantom}`]);
  });

  it("captures nested var() fallbacks, and clears references to real DS tokens", () => {
    // A nested fallback yields BOTH names — the outer first-arg and the inner var().
    const nested = "var(" + "--pl-a" + ", " + "var(" + "--pl-b" + "))";
    expect(refsIn(nested)).toEqual(["--pl-a", "--pl-b"]);
    // A line of only real tokens (incl. a nested fallback) is matched but clears the sweep.
    const real = "background: " + "var(" + "--pl-color-" + "bg-inset, " + "var(" + "--pl-color-" + "bg));";
    expect(refsIn(real).every((n) => defined.has(n))).toBe(true);
  });

  it("exempts the absence-asserting files; the guard itself is the importer, so it isn't swept", () => {
    expect(EXEMPT.size).toBe(3);
    // The two OTHER absence-asserting files must be visible to the sweep, so skipping them matters.
    expect(Object.keys(SOURCES)).toContain("./statusTokenGuard.test.ts");
    expect(Object.keys(SOURCES)).toContain("../chat/__tests__/hitl-accent.test.ts");
    // Vite omits the importing module from its own glob, so this guard is never swept at all —
    // its concat-built literals can't self-flag independent of the explicit self-exemption above.
    expect(Object.keys(SOURCES)).not.toContain("./tokenNameGuard.test.ts");
  });
});

// ── #3685 (final): two tree-wide colour-hygiene sweeps ────────────────────────────────────────
// Parts (a)–(d) stripped `var(--pl-…, #hex)` fallbacks and the legacy brand-* accent aliases
// sheet-by-sheet, each pinned by its own per-file guard. This closes #3685 by (1) deleting the
// last brand-* definitions from theme-base.css and (2) turning both file-scoped invariants into
// tree-wide ones: either regression now fails here the moment it lands in a shipped surface.
//
// Both reuse the SOURCES ?raw glob above (compile-time, rooted at this file, no node:fs) and
// report sorted `src/<path>:<line>` exactly as the phantom sweep does.
const prettyPath = (file: string): string =>
  file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");

// (a) NO HEX FALLBACK. main.tsx imports @protolabsai/design before any app CSS, so a --pl-* token
// always resolves; a `var(--pl-name, #hex)` fallback can therefore only ever paint a wrong,
// dark-only colour once the token exists (the exact failure #3682/#3685 chased out). `var(` is
// escaped so this string is never itself a bare `var(--pl-…)`, and the hex class carries no real
// digits, so the pattern holds no offending literal of its own.
const HEX_FALLBACK_SRC = "var\\(\\s*--pl-[\\w-]+\\s*,\\s*#[0-9a-fA-F]{3,8}\\b";
const hasHexFallback = (line: string): boolean => new RegExp(HEX_FALLBACK_SRC).test(line);

// EXEMPT from the hex rule, and WHY (there is no allowlist of specific stragglers — these are
// principled categories):
//  • ./app-crash.css — the last-resort crash screen must still render if the token stylesheet
//    failed to load, so its fallbacks are load-bearing (also pinned by phantomTokenRename.test.ts).
//  • *.test.ts / *.test.tsx — absence-asserting guards (this file, phantomTokenRename,
//    dsTokenFallbackStrip[B], chat-css-tokens, …) must spell the exact `var(--pl-…, #hex)` string
//    to prove it is gone. Those are test fixtures — the one place the card sanctions hex literals.
// theme-base.css is NO LONGER exempt: the DS audit's stale-fallback cleanup (card 1a) dropped its
// status-alias hex fallbacks, so it is swept like every other shipped sheet — its status aliases
// now read --pl-color-status-* tokens with no literal fallback (statusTokenGuard.test.ts and the
// brand-alias guard below still pin that the --success/--info aliases exist).
const hexExempt = (file: string): boolean =>
  file === "./app-crash.css" || /\.test\.tsx?$/.test(file);

function hexSweep(): string[] {
  const hits: string[] = [];
  for (const [file, raw] of Object.entries(SOURCES)) {
    if (hexExempt(file)) continue;
    stripComments(raw, file.endsWith(".css")).split("\n").forEach((line, i) => {
      if (hasHexFallback(line)) hits.push(`${prettyPath(file)}:${i + 1}`);
    });
  }
  return hits.sort();
}

// (b) NO brand-* alias. The legacy accent aliases (brand-violet/-light, brand-indigo/-bright,
// brand-pink) are all re-pointed onto real DS tokens, and #3685 (final) deletes the last
// definitions from theme-base.css, so the name is fully retired: nothing but this guard may name
// it. The literal is assembled by concat so the guard holds no bare occurrence of its own, and —
// unlike the hex rule — the scan runs over RAW text (comments included), mirroring the acceptance
// grep for the retired prefix across apps/web/src: a stray mention even in prose is a straggler.
// Only this guard file is exempt.
const BRAND_LITERAL = "--" + "brand-";
function brandSweep(): string[] {
  const hits: string[] = [];
  for (const [file, raw] of Object.entries(SOURCES)) {
    if (file === "./tokenNameGuard.test.ts") continue; // the guard itself (also omitted by Vite)
    raw.split("\n").forEach((line, i) => {
      if (line.includes(BRAND_LITERAL)) hits.push(`${prettyPath(file)}:${i + 1}`);
    });
  }
  return hits.sort();
}

describe("no var(--pl-…, #hex) fallback ships outside app-crash / test fixtures (#3685)", () => {
  it("sweeps the tree: shipped CSS/TS/TSX carries no hex fallback", () => {
    expect(hexSweep()).toEqual([]);
  });

  it("flags a synthetic hex fallback and reports src/<path>:<line> (meta-guard, built by concat)", () => {
    // The offending shapes, assembled so this file holds no bare `var(--pl-…, #hex)` literal.
    expect(hasHexFallback("color: " + "var(" + "--pl-color-accent, #" + "7c8cff);")).toBe(true);
    expect(
      hasHexFallback("border: 1px solid " + "var(" + "--pl-color-border, #" + "2a2a31);"),
    ).toBe(true);
    // A bare token, a nested var() fallback and a font-family list all clear the rule.
    expect(hasHexFallback("color: " + "var(" + "--pl-color-accent);")).toBe(false);
    expect(
      hasHexFallback("background: " + "var(" + "--pl-color-bg-inset, " + "var(" + "--pl-color-bg))"),
    ).toBe(false);
    expect(hasHexFallback("font-family: " + "var(" + "--pl-font-mono, ui-monospace, monospace)")).toBe(false);
    // Prove the file:line shape a real hit would take over a two-line synthetic source.
    const synthetic = [
      "ok: " + "var(" + "--pl-color-fg);",
      "bad: " + "var(" + "--pl-color-fg, #" + "ededed);",
    ].join("\n");
    const hits: string[] = [];
    synthetic.split("\n").forEach((l, i) => {
      if (hasHexFallback(l)) hits.push(`src/synthetic.css:${i + 1}`);
    });
    expect(hits).toEqual(["src/synthetic.css:2"]);
  });

  it("exempts app-crash / test fixtures, but sweeps shipped surfaces incl. theme-base — all present", () => {
    expect(hexExempt("./app-crash.css")).toBe(true);
    expect(hexExempt("./theme-base.css")).toBe(false); // no longer exempt — card 1a dropped its hex fallbacks
    expect(hexExempt("../chat/chat-css-tokens.test.ts")).toBe(true); // a test fixture
    expect(hexExempt("../chat/chat.css")).toBe(false); // a shipped surface IS swept
    expect(hexExempt("./ProtoLabsIcon.tsx")).toBe(false);
    // The whole-file exemption (app-crash) and the now-swept theme-base must both be in the tree
    // (a glob typo would hide them, quietly blinding the sweep).
    expect(Object.keys(SOURCES)).toContain("./app-crash.css");
    expect(Object.keys(SOURCES)).toContain("./theme-base.css");
  });
});

describe("the retired brand-* accent aliases appear nowhere but this guard (#3685)", () => {
  it("sweeps the tree: no brand alias definition or reference survives", () => {
    expect(brandSweep()).toEqual([]);
  });

  it("flags a synthetic brand alias and reports src/<path>:<line> (meta-guard, built by concat)", () => {
    const ref = "color: " + "var(" + "--" + "brand-violet);"; // a reference
    const def = "  --" + "brand-pink: " + "var(--pl-color-accent);"; // a definition
    expect(ref.includes(BRAND_LITERAL)).toBe(true);
    expect(def.includes(BRAND_LITERAL)).toBe(true);
    // A real DS token clears the rule.
    expect(("color: " + "var(" + "--pl-color-accent);").includes(BRAND_LITERAL)).toBe(false);
    const synthetic = [
      "ok: " + "var(" + "--pl-color-accent);",
      "bad: " + "var(" + "--" + "brand-indigo);",
    ].join("\n");
    const hits: string[] = [];
    synthetic.split("\n").forEach((l, i) => {
      if (l.includes(BRAND_LITERAL)) hits.push(`src/synthetic.css:${i + 1}`);
    });
    expect(hits).toEqual(["src/synthetic.css:2"]);
  });

  it("theme-base.css keeps its status compat aliases but defines no brand alias (last ones deleted)", () => {
    const themeBase = SOURCES["./theme-base.css"];
    expect(themeBase.includes(BRAND_LITERAL)).toBe(false);
    // The sanctioned status aliases it still owns are untouched (statusTokenGuard.test.ts pins them).
    expect(themeBase).toContain("--success: " + "var(--pl-color-status-success");
    expect(themeBase).toContain("--info: " + "var(--pl-color-status-info");
  });
});

// ── DS audit @ c5b74b26 (stale-fallback, final): NO var(--pl-…, <literal>) fallback ships ──────
// The closing card of the stale-fallback cleanup. Cards 1a–1f dropped every literal fallback from
// apps/web/src, one surface at a time; this turns that into a tree-wide invariant, strictly WIDER
// than the #3685 hex sweep above. main.tsx imports @protolabsai/design before any app CSS, so a
// --pl-* token ALWAYS resolves — any `var(--pl-name, <literal>)` fallback is dead weight that can
// only ever paint a wrong, theme-deaf value if a token momentarily resolves to nothing. So NO
// literal fallback of ANY kind (hex, rgba()/color-mix, a font stack, a shadow, `inherit`, a px
// value) may ship. The one legal shape is a NESTED TOKEN fallback whose value is ENTIRELY another
// `var(--pl-…)` read (e.g. var(--pl-color-fg-subtle, var(--pl-color-fg-muted))): those chain a
// finer token onto a coarser one and are used intentionally across the tree.
//
// Exemptions (only two):
//  • ./app-crash.css — the root error-boundary crash screen (#872) must render even when the token
//    stylesheet failed to load, so ITS literal fallbacks are load-bearing (whole file).
//  • *.test.ts / *.test.tsx — absence-asserting fixtures (this file included) that must spell the
//    exact `var(--pl-…, <literal>)` string to prove it is gone.
// theme-base.css is NOT exempt (nor from the hex rule any longer): card 1a dropped its status-alias
// literal fallbacks, so it is swept like every other shipped sheet.
const fallbackExempt = (file: string): boolean =>
  file === "./app-crash.css" || /\.test\.tsx?$/.test(file);

// Given text and the index of an opening "(", return the index of its matching ")", or -1 if
// unbalanced. Balanced so a `var(--pl-x, rgba(…))` or `… 0 2px 8px rgba(…)` shadow fallback is
// consumed WHOLE instead of a first-`)` regex stopping inside the inner rgba()/color-mix().
function matchParen(text: string, openIdx: number): number {
  let depth = 0;
  for (let i = openIdx; i < text.length; i += 1) {
    if (text[i] === "(") depth += 1;
    else if (text[i] === ")") {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return -1;
}

// True when a fallback argument (already sliced out of its var()) is ENTIRELY a single nested
// `var(--pl-…)` read — the one sanctioned fallback shape. `var(` is split from `--pl-` so this
// source carries no bare literal of its own. A nested var() that itself trails a literal (or more
// values) is NOT entirely one var() read, so it fails here — and the inner var() is caught on its
// own by the scan below anyway.
const NESTED_HEAD = new RegExp("^var\\(\\s*" + "--pl-" + "[a-z0-9_-]+");
function isNestedTokenFallback(fallback: string): boolean {
  const t = fallback.trim();
  if (!NESTED_HEAD.test(t)) return false;
  return matchParen(t, t.indexOf("(")) === t.length - 1;
}

// Scan one comment-stripped source for every var(--pl-…, <fallback>) whose fallback is NOT a nested
// token read, formatted `<pretty>:<line>`. Reused by the tree sweep and the synthetic meta-tests so
// both exercise the identical detector. The global VAR_REF matcher visits every var(--pl-… — the
// outer read AND any nested one — so a nested var() carrying its own literal is flagged in place.
function scanFallbacks(text: string, pretty: string): string[] {
  const hits: string[] = [];
  const head = new RegExp(VAR_REF, "g");
  let m: RegExpExecArray | null;
  while ((m = head.exec(text)) !== null) {
    let j = m.index + m[0].length; // just past the --pl-name
    while (j < text.length && /\s/.test(text[j])) j += 1;
    if (text[j] !== ",") continue; // no fallback → a bare token read, always legal
    const close = matchParen(text, m.index + m[0].indexOf("(")); // balanced-paren end of this var()
    if (close === -1) continue; // unbalanced (never happens in valid CSS)
    if (isNestedTokenFallback(text.slice(j + 1, close))) continue; // nested token fallback stays legal
    hits.push(`${pretty}:${text.slice(0, m.index).split("\n").length}`);
  }
  return hits;
}

function fallbackSweep(): string[] {
  const hits: string[] = [];
  for (const [file, raw] of Object.entries(SOURCES)) {
    if (fallbackExempt(file)) continue;
    hits.push(...scanFallbacks(stripComments(raw, file.endsWith(".css")), prettyPath(file)));
  }
  return hits.sort();
}

describe("no var(--pl-…, <literal>) fallback ships outside app-crash / test fixtures (DS audit stale-fallback, final)", () => {
  it("sweeps the tree: shipped CSS/TS/TSX carries no literal fallback, only nested token fallbacks", () => {
    expect(fallbackSweep()).toEqual([]);
  });

  it("flags every literal-fallback shape but clears bare tokens and nested token fallbacks (built by concat)", () => {
    // Each offending shape, assembled so this file holds no bare `var(--pl-…, <literal>)` of its own.
    const flagged = [
      "font-family: " + "var(" + "--pl-font-mono, ui-monospace, SFMono-Regular, monospace);", // a mono font stack
      "content: " + "var(" + "--pl-color-fg" + ", inherit);", // `, inherit`
      "background: " + "var(" + "--pl-color-bg, rgba(0, 0, 0, 0.5));", // an rgba() fallback
      "box-shadow: " + "var(" + "--pl-shadow-1, 0 2px 8px rgba(0, 0, 0, 0.3));", // a shadow fallback
      "gap: " + "var(" + "--pl-space-4, 16px);", // a bare-literal px fallback
    ];
    for (const line of flagged) expect(scanFallbacks(line, "x"), line).toHaveLength(1);

    const clear = [
      "color: " + "var(" + "--pl-color-accent);", // a bare token, no fallback
      "color: " + "var(" + "--pl-color-fg-subtle, " + "var(" + "--pl-color-fg-muted));", // a nested token fallback
    ];
    for (const line of clear) expect(scanFallbacks(line, "x"), line).toEqual([]);
  });

  it("proves isNestedTokenFallback: a whole-var() fallback passes, a var()-plus-literal does not", () => {
    expect(isNestedTokenFallback(" " + "var(" + "--pl-color-fg-muted)")).toBe(true);
    expect(isNestedTokenFallback("rgba(0, 0, 0, 0.5)")).toBe(false);
    // A nested var() that trails another value is not ENTIRELY one var() read.
    expect(isNestedTokenFallback("var(" + "--pl-color-fg) 2px")).toBe(false);
  });

  it("catches a balanced rgba() fallback whole and reports sorted src/<path>:<line> over a synthetic source", () => {
    const synthetic = [
      "ok: " + "var(" + "--pl-color-fg);", // bare token
      "ok2: " + "var(" + "--pl-color-fg-subtle, " + "var(" + "--pl-color-fg-muted));", // nested token fallback
      "bad: " + "background: " + "var(" + "--pl-color-bg, rgba(0, 0, 0, 0.5));", // literal fallback → hit
    ].join("\n");
    // The rgba() is consumed whole (its inner `)` does not end the var()), and only line 3 is a hit.
    expect(scanFallbacks(synthetic, "src/synthetic.css").sort()).toEqual(["src/synthetic.css:3"]);
  });

  it("a nested var() carrying its own literal is flagged in place (the inner read, not the outer)", () => {
    // Outer fallback `var(--pl-b, #fff)` is entirely a var() so the OUTER clears, but the inner
    // `var(--pl-b, #fff)` is visited on its own and flagged for the trailing #fff → exactly one hit.
    const line = "color: " + "var(" + "--pl-a, " + "var(" + "--pl-b, #" + "fff));";
    expect(scanFallbacks(line, "x")).toHaveLength(1);
  });

  it("exempts app-crash and test fixtures, sweeps everything else incl. theme-base — all present", () => {
    expect(fallbackExempt("./app-crash.css")).toBe(true);
    expect(fallbackExempt("../chat/chat-css-tokens.test.ts")).toBe(true); // a test fixture
    expect(fallbackExempt("./theme-base.css")).toBe(false); // theme-base is swept now
    expect(fallbackExempt("../chat/chat.css")).toBe(false); // a shipped surface IS swept
    expect(fallbackExempt("./ProtoLabsIcon.tsx")).toBe(false);
    // The whole-file exemption must actually be in the swept tree (a glob typo would hide it).
    expect(Object.keys(SOURCES)).toContain("./app-crash.css");
    expect(Object.keys(SOURCES)).toContain("./theme-base.css");
  });
});
