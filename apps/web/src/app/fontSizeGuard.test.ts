import { describe, expect, it } from "vitest";

// #3688 (type-scale, final): the regression guard that closes the literal-`font-size`
// class of drift. The DS ships a fixed type scale — `--pl-font-size-{3xs,2xs,xs,sm,base,
// lg,xl}` (10/11/12/13/14/16/18px) — and the seven migration cards moved every in-range
// `font-size: <n>px` in the console CSS onto those tokens. A raw px `font-size` that comes
// back is a site that no longer follows the operator's chosen density / accessibility zoom
// (the tokens can be rescaled at the DS root; a hardcoded px cannot). This sweeps every
// apps/web/src stylesheet and fails on any literal px `font-size` declaration, including a
// custom property whose name ends in `font-size` (e.g. `--diffs-font-size: 12px`).
//
// Vite `?raw` globs rather than node:fs: this tsconfig has no node types and under jsdom
// `import.meta.url` is an http: URL, so URL-relative filesystem access is a trap (same
// reasoning as statusTokenGuard.test.ts / tokenNameGuard.test.ts). The glob is compile-time,
// rooted at this file in src/app, and picks up new stylesheets automatically. The guard is a
// `.ts` file and the glob only matches `.css`, so it is never swept — its example literals
// are also assembled by concat (below) so no bare `font-size: <n>px` lives here to self-flag.
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): same-directory files key as
// `./name`, files in sibling directories as `../dir/name`.
const APP_CRASH = "./app-crash.css";
const DEVICES = "../settings/devices.css";

// Report path: turn the importer-relative glob key into a repo-relative `src/<path>`.
const pretty = (file: string): string =>
  file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");

// A literal px `font-size` declaration. The optional `--[\w-]*` prefix (backtracked so the
// trailing `font-size` still matches) also catches custom properties whose name ends in
// `font-size`, e.g. `--diffs-font-size: 12px`. The leading boundary keeps `-webkit-…` and
// mid-word hits from matching, and clears token reads (`font-size: var(--pl-font-size-xs)`)
// and unit-relative values (`font-size: 1em`) since neither is `[0-9.]+px`. Built from a
// RegExp *string* so this file holds no bare `font-size: <n>px` of its own. Group 1 is the
// whole declaration (for the report), group 2 the numeric value (for the pinned exemption).
const FONT_SIZE_PX_SRC = "(?:^|[;{\\s])((?:--[\\w-]*)?font-size\\s*:\\s*([0-9.]+)px)";
function pxFontSizes(line: string): { decl: string; value: string }[] {
  const out: { decl: string; value: string }[] = [];
  for (const m of line.matchAll(new RegExp(FONT_SIZE_PX_SRC, "g"))) {
    out.push({ decl: m[1], value: `${m[2]}px` });
  }
  return out;
}

// Strip `/* … */` comments before sweeping so a px `font-size` shown in a comment (prose,
// a legacy note, a migration TODO) is not mistaken for a live declaration. Replaced by their
// own newline count so reported line numbers stay true to the original file.
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => "\n".repeat((m.match(/\n/g) ?? []).length));
}

// For each line, the selector of the innermost open rule — a lightweight brace/selector stack
// over the comment-stripped text, used to PIN the devices.css exemption to its rule rather
// than exempting a bare value anywhere in the file. `;` ends a declaration, `{` opens a rule
// (its selector is the text accumulated since the last brace/`;`, trimmed), `}` closes one.
// Selectors may span lines, so the buffer is not reset at newlines. byLine is 1-based.
function enclosingSelectors(text: string): string[] {
  const byLine: string[] = [""];
  const stack: string[] = [];
  const top = (): string => (stack.length ? stack[stack.length - 1] : "");
  let pending = "";
  let line = 1;
  byLine[line] = top();
  for (const ch of text) {
    if (ch === "\n") {
      line += 1;
      byLine[line] = top();
      continue;
    }
    if (ch === "{") {
      stack.push(pending.trim());
      pending = "";
    } else if (ch === "}") {
      stack.pop();
      pending = "";
    } else if (ch === ";") {
      pending = "";
    } else {
      pending += ch;
    }
    byLine[line] = top();
  }
  return byLine;
}

// EXEMPTIONS — exactly three, each with a documented reason. No other allowlist entries: a
// straggler the migration cards missed must be TOKENIZED, not exempted.
//
//  1. app/app-crash.css — the WHOLE file. It is the root error-boundary fallback (#872) and
//     must render even if the DS token CSS failed to load, so it keeps hex colour fallbacks
//     for the same reason (see tokenNameGuard.test.ts). A `var(--pl-font-size-*)` there could
//     resolve to nothing on that path, so its px `font-size` values are load-bearing. Skipped
//     as a whole file below.
//
//  2. settings/devices.css `.devices-code code` — the pairing-code display: `28px`, and its
//     `@media (max-width: 767px)` override `22px`. This sits OUTSIDE the DS 10–18px scale; it
//     is a DS gap filed as protoLabsAI/protoContent#534 (a proposed `--pl-font-size-display`
//     step). Remove this exemption and migrate BOTH sites once protoContent#534 ships to npm
//     (designSystem will brief that follow-up). Pinned by selector + value below (NOT the
//     whole file) so any OTHER literal `font-size` in devices.css still fails.
//
//  3. This guard file — a `.ts`, never in the `.css` glob, and its example literals are built
//     by concat, so it cannot flag itself.
function isExempt(file: string, selector: string, value: string): boolean {
  // protoContent#534 — the `.devices-code code` pairing-code display, base + mobile override.
  return file === DEVICES && selector === ".devices-code code" && (value === "28px" || value === "22px");
}

// Every offending declaration in one stylesheet, as `src/<path>:<line> <declaration>`.
function offendersIn(file: string, raw: string): string[] {
  if (file === APP_CRASH) return []; // whole-file exemption (root crash fallback, #872)
  const stripped = stripComments(raw);
  const selectors = enclosingSelectors(stripped);
  const hits: string[] = [];
  stripped.split("\n").forEach((lineText, i) => {
    const lineNo = i + 1;
    for (const { decl, value } of pxFontSizes(lineText)) {
      if (isExempt(file, selectors[lineNo] ?? "", value)) continue;
      hits.push(`${pretty(file)}:${lineNo} ${decl}`);
    }
  });
  return hits;
}

// Sorted `src/<path>:<line> <declaration>` across the whole console CSS tree.
function sweep(): string[] {
  return Object.entries(CSS_SOURCES)
    .flatMap(([file, raw]) => offendersIn(file, raw))
    .sort();
}

describe("no literal px font-size in the console CSS (#3688 type-scale final)", () => {
  it("sweeps the tree: every font-size reads a DS token, not a px literal", () => {
    expect(sweep()).toEqual([]);
  });

  it("reads real stylesheet text — a stubbed (empty) css import would blind the sweep", () => {
    // apps/web/src CSS is opted into processing by vitest.config.ts `css.include`; if that
    // regresses every ?raw css import returns "" and the sweep passes on nothing. Assert each
    // src stylesheet is non-empty, and floor the tree size so a glob typo can't sweep an empty set.
    for (const [file, text] of Object.entries(CSS_SOURCES)) {
      expect(text.length, `${file} imported empty — widen css.include in vitest.config.ts`).toBeGreaterThan(0);
    }
    expect(Object.keys(CSS_SOURCES).length).toBeGreaterThan(20);
  });

  it("the matcher flags literal px font-sizes, including custom properties ending in font-size", () => {
    // Literals assembled by concat so this file holds no bare `font-size: <n>px`.
    expect(pxFontSizes("  font-size: 12" + "px;").map((h) => h.value)).toEqual(["12px"]);
    expect(pxFontSizes("  --diffs-font-size: 12" + "px;").map((h) => h.value)).toEqual(["12px"]);
    // Half-pixel values are literals too (the migration snapped them to steps).
    expect(pxFontSizes("  font-size: 13.5" + "px;").map((h) => h.value)).toEqual(["13.5px"]);
  });

  it("the matcher ignores token reads, unit-relative values, and px inside a comment", () => {
    expect(pxFontSizes("  font-size: " + "var(--pl-font-size-xs);")).toEqual([]);
    expect(pxFontSizes("  font-size: 1em;")).toEqual([]);
    // The sweep strips comments first, so a px font-size shown in a comment is not a hit.
    const commented = "/* font-size: 13" + "px (legacy) */\n.x { font-size: " + "var(--pl-font-size-sm); }";
    expect(offendersIn("synthetic.css", commented)).toEqual([]);
  });

  it("reports src/<path>:<line> <declaration> for a real offender (meta-guard, built by concat)", () => {
    const bad = ".x {\n  font-size: 12" + "px;\n}";
    expect(offendersIn("synthetic.css", bad)).toEqual(["synthetic.css:2 font-size: 12px"]);
    const badVar = ":root {\n  --diffs-font-size: 12" + "px;\n}";
    expect(offendersIn("synthetic.css", badVar)).toEqual(["synthetic.css:2 --diffs-font-size: 12px"]);
  });

  it("pins the devices.css exemption to selector + value, not the whole file (protoContent#534)", () => {
    // The two sanctioned DS-gap sites are exempt …
    expect(isExempt(DEVICES, ".devices-code code", "28px")).toBe(true);
    expect(isExempt(DEVICES, ".devices-code code", "22px")).toBe(true);
    // … but ANY other value or selector in devices.css still fails, and the pin doesn't leak
    // to other files (a hostile 28px elsewhere is not waved through).
    expect(isExempt(DEVICES, ".devices-code code", "13px")).toBe(false);
    expect(isExempt(DEVICES, ".devices-list code", "28px")).toBe(false);
    expect(isExempt("../chat/chat.css", ".devices-code code", "28px")).toBe(false);
    // Proven end to end over synthetic devices.css text: the two gap sites clear, a stray
    // literal under a different selector is reported.
    const devicesLike =
      ".devices-code code {\n  font-size: 28" + "px;\n}\n" +
      "@media (max-width: 767px) {\n  .devices-code code {\n    font-size: 22" + "px;\n  }\n}\n" +
      ".devices-other {\n  font-size: 30" + "px;\n}";
    expect(offendersIn(DEVICES, devicesLike)).toEqual(["src/settings/devices.css:10 font-size: 30px"]);
  });

  it("covers the tree and both exempt files are actually in the swept glob", () => {
    const cssFiles = Object.keys(CSS_SOURCES);
    // A glob typo that silently matched nothing would fail here instead of passing vacuously.
    expect(cssFiles.length).toBeGreaterThan(20);
    // The whole-file and pinned exemptions only matter if the files are really swept.
    expect(cssFiles).toContain(APP_CRASH);
    expect(cssFiles).toContain(DEVICES);
    // The guard is a .ts file, so the .css-only glob never includes it — it cannot self-flag.
    expect(cssFiles).not.toContain("./fontSizeGuard.test.ts");
  });

  it("tracks the innermost selector across nested @media rules", () => {
    const css = "@media (max-width: 767px) {\n  .devices-code code {\n    font-size: 22" + "px;\n  }\n}";
    const selectors = enclosingSelectors(stripComments(css));
    expect(selectors[3]).toBe(".devices-code code");
  });
});
