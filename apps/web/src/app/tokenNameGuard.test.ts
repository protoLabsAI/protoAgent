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
const DECL = /(--pl-[a-z0-9-]+)\s*:/g;
const declaredIn = (text: string): string[] => [...text.matchAll(DECL)].map((m) => m[1]);

// The DEFINED set — every --pl-* the installed design system declares. Derived from the
// installed packages, never hand-copied. Four live sources, unioned:
//   1. PL_TOKEN_VARS — @protolabsai/design's tokens.json, flattened by PluginView exactly the
//      way the DS build emits tokens.css. This is the load-bearing source at test time (~74
//      names on design ^0.10.0).
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
const VAR_REF = "var\\(\\s*(--pl-[a-z0-9-]+)";
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
    for (const n of ["--pl-color-fg", "--pl-color-accent", "--pl-color-status-error", "--pl-radius"]) {
      expect(defined.has(n)).toBe(true);
    }
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
