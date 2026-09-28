import { describe, expect, it } from "vitest";

// #3688 (type-scale, final GUARD): the regression guard that closes the literal-`font-size`
// class of drift, in BOTH the console CSS and inline React styles. The DS ships a fixed type
// scale — `--pl-font-size-{3xs,2xs,xs,sm,base,lg,xl}` (10/11/12/13/14/16/18px) — and the
// migration cards moved every in-range CSS `font-size` (2a/2b) and the inline `fontSize`
// literals onto those tokens. A raw literal that comes back is a site that no longer follows
// the operator's chosen density / accessibility zoom (the tokens can be rescaled at the DS
// root; a hardcoded literal cannot). This sweeps:
//   • every apps/web/src stylesheet — failing on any literal `font-size: <n>px` OR `<n>rem` /
//     `<n>em` declaration, including a custom property whose name ends in `font-size`
//     (e.g. `--diffs-font-size: 12px`);
//   • every non-test TSX under apps/web/src — failing on any inline `fontSize:` whose value is a
//     numeric literal (`fontSize: 13`) or a string ending in px/rem/em (`fontSize: "0.8rem"`),
//     while allowing a token read (`fontSize: "var(--pl-font-size-sm)"`) and non-length
//     theme-blob values (`fontSize: "lg"`, see src/lib/themeMerge — those aren't style literals).
//
// Vite `?raw` globs rather than node:fs: this tsconfig has no node types and under jsdom
// `import.meta.url` is an http: URL, so URL-relative filesystem access is a trap (same
// reasoning as statusTokenGuard.test.ts / tokenNameGuard.test.ts). The globs are compile-time,
// rooted at this file in src/app, and pick up new files automatically. This guard is a `.ts`
// file matched by neither glob (`.css` / `.tsx`), and its example literals are assembled by
// concat (below), so it can never self-flag.
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Non-test TSX. The glob matches every `.tsx` under src (including `*.test.tsx`); the test
// files are filtered out below so a test's synthetic `fontSize: 13` fixture never trips the
// sweep. Keyed importer-relative to src/app, same as CSS_SOURCES.
const TSX_SOURCES = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>,
  ).filter(([file]) => !file.endsWith(".test.tsx")),
);

// Glob keys are importer-relative (this file lives in src/app): same-directory files key as
// `./name`, files in sibling directories as `../dir/name`.
const APP_CRASH = "./app-crash.css";
const DEVICES = "../settings/devices.css";
const CHAT = "../chat/chat.css";

// Report path: turn the importer-relative glob key into a repo-relative `src/<path>`.
const pretty = (file: string): string =>
  file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");

// A literal `font-size` declaration in the length units the DS type scale owns: absolute `px`
// and the unit-relative `rem`/`em`. The optional `--[\w-]*` prefix (backtracked so the trailing
// `font-size` still matches) also catches custom properties whose name ends in `font-size`,
// e.g. `--diffs-font-size: 12px`. The leading boundary keeps `-webkit-…` and mid-word hits from
// matching, and clears token reads (`font-size: var(--pl-font-size-xs)`) and keyword values
// (`font-size: inherit`) since neither is `[0-9.]+(px|rem|em)`. `rem` precedes `em` in the
// alternation so `0.8rem` captures `rem`, not a trailing `em`. Built from a RegExp *string* so
// this file holds no bare `font-size: <n><unit>` of its own. Group 1 is the whole declaration
// (for the report), group 2 the numeric value WITH its unit (for the pinned exemptions).
const FONT_SIZE_LITERAL_SRC = "(?:^|[;{\\s])((?:--[\\w-]*)?font-size\\s*:\\s*([0-9.]+(?:px|rem|em)))";
function fontSizeLiterals(line: string): { decl: string; value: string }[] {
  const out: { decl: string; value: string }[] = [];
  for (const m of line.matchAll(new RegExp(FONT_SIZE_LITERAL_SRC, "g"))) {
    out.push({ decl: m[1], value: m[2] });
  }
  return out;
}

// An inline `fontSize:` style value that is a raw literal: an unquoted number (`fontSize: 13`)
// or a quoted string ending in px/rem/em (`fontSize: "0.8rem"`). A token read
// (`fontSize: "var(--pl-font-size-sm)"`) and a non-length theme-blob value (`fontSize: "lg"`)
// both fail the inner alternation, so neither is flagged. The leading boundary (`{`, `,`,
// whitespace, or line start) keeps a longer identifier ending in `fontSize`
// (`defaultFontSize: 13`) from matching. Built from a RegExp *string*; group 1 is the whole
// `fontSize: <literal>` declaration (for the report), group 2 backreferences the string quote.
const FONT_SIZE_TSX_SRC = "(?:^|[\\s{,])(fontSize\\s*:\\s*(?:([\"'])[0-9.]+(?:px|rem|em)\\2|[0-9.]+))";
function inlineFontSizeLiterals(line: string): string[] {
  const out: string[] = [];
  for (const m of line.matchAll(new RegExp(FONT_SIZE_TSX_SRC, "g"))) out.push(m[1]);
  return out;
}

// Strip `/* … */` comments before sweeping so a font-size shown in a comment (prose, a legacy
// note, a migration TODO) is not mistaken for a live declaration. Replaced by their own newline
// count so reported line numbers stay true to the original file.
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => "\n".repeat((m.match(/\n/g) ?? []).length));
}

// TSX carries both comment forms. Strip `/* … */` blocks (JSDoc, JSX `{/* … */}`) first,
// line-count-preserving, then `//` line comments to end of line — so a `fontSize` in a doc
// example or a commented-out style is prose, not a live declaration. (A `//` inside a same-line
// string is over-stripped, but a real inline `fontSize` literal never shares a line with one.)
function stripTsxComments(text: string): string {
  return stripComments(text)
    .split("\n")
    .map((line) => line.replace(/\/\/.*$/, ""))
    .join("\n");
}

// For each line, the selector of the innermost open rule — a lightweight brace/selector stack
// over the comment-stripped text, used to PIN the devices.css / chat.css exemptions to their
// rule rather than exempting a bare value anywhere in the file. `;` ends a declaration, `{`
// opens a rule (its selector is the text accumulated since the last brace/`;`, trimmed), `}`
// closes one. Selectors may span lines, so the buffer is not reset at newlines. byLine is 1-based.
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

// EXEMPTIONS — each with a documented reason. No other allowlist entries: a straggler the
// migration cards missed must be TOKENIZED, not exempted.
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
//  3. chat/chat.css `.chat-user-text.chat-slash-cmd` — the inline mono slash-command chip
//     (#1529): `0.9em`. It is inline code (a "/foo" command rendered inside the user bubble's
//     text), and designSystem's type-scale brief sanctions an em-relative size THERE so the
//     chip tracks the surrounding message text rather than pinning to an absolute scale step.
//     Pinned by selector + value (NOT the whole file) so any other literal in chat.css fails.
//
//  4. This guard file — a `.ts`, matched by neither the `.css` nor the `.tsx` glob, and its
//     example literals are built by concat, so it cannot flag itself.
function isExempt(file: string, selector: string, value: string): boolean {
  // protoContent#534 — the `.devices-code code` pairing-code display, base + mobile override.
  if (file === DEVICES && selector === ".devices-code code" && (value === "28px" || value === "22px")) {
    return true;
  }
  // #1529 — the inline mono slash-command chip; em-relative is sanctioned for inline code.
  if (file === CHAT && selector === ".chat-user-text.chat-slash-cmd" && value === "0.9em") return true;
  return false;
}

// Every offending CSS declaration in one stylesheet, as `src/<path>:<line> <declaration>`.
function offendersIn(file: string, raw: string): string[] {
  if (file === APP_CRASH) return []; // whole-file exemption (root crash fallback, #872)
  const stripped = stripComments(raw);
  const selectors = enclosingSelectors(stripped);
  const hits: string[] = [];
  stripped.split("\n").forEach((lineText, i) => {
    const lineNo = i + 1;
    for (const { decl, value } of fontSizeLiterals(lineText)) {
      if (isExempt(file, selectors[lineNo] ?? "", value)) continue;
      hits.push(`${pretty(file)}:${lineNo} ${decl}`);
    }
  });
  return hits;
}

// Every offending inline `fontSize` literal in one TSX file, same report shape as the CSS sweep.
function tsxOffendersIn(file: string, raw: string): string[] {
  const stripped = stripTsxComments(raw);
  const hits: string[] = [];
  stripped.split("\n").forEach((lineText, i) => {
    for (const decl of inlineFontSizeLiterals(lineText)) {
      hits.push(`${pretty(file)}:${i + 1} ${decl}`);
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

// Sorted `src/<path>:<line> <declaration>` across every non-test TSX file.
function sweepTsx(): string[] {
  return Object.entries(TSX_SOURCES)
    .flatMap(([file, raw]) => tsxOffendersIn(file, raw))
    .sort();
}

describe("no literal font-size in the console CSS or inline styles (#3688 type-scale final)", () => {
  it("sweeps the CSS tree: every font-size reads a DS token, not a px/rem/em literal", () => {
    expect(sweep()).toEqual([]);
  });

  it("sweeps the TSX tree: every inline fontSize reads a DS token, not a literal", () => {
    expect(sweepTsx()).toEqual([]);
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

  it("reads real TSX text and the .tsx glob is non-vacuous (>20 non-test files)", () => {
    for (const [file, text] of Object.entries(TSX_SOURCES)) {
      expect(text.length, `${file} imported empty`).toBeGreaterThan(0);
      expect(file.endsWith(".test.tsx"), `${file} is a test file — filter it out`).toBe(false);
    }
    // A glob typo that silently matched nothing would fail here instead of passing vacuously.
    expect(Object.keys(TSX_SOURCES).length).toBeGreaterThan(20);
  });

  it("the CSS matcher flags px, rem and em font-sizes, including custom properties", () => {
    // Literals assembled by concat so this file holds no bare `font-size: <n><unit>`.
    expect(fontSizeLiterals("  font-size: 12" + "px;").map((h) => h.value)).toEqual(["12px"]);
    expect(fontSizeLiterals("  --diffs-font-size: 12" + "px;").map((h) => h.value)).toEqual(["12px"]);
    // Half-pixel values are literals too (the migration snapped them to steps).
    expect(fontSizeLiterals("  font-size: 13.5" + "px;").map((h) => h.value)).toEqual(["13.5px"]);
    // rem and em are now flagged too (2a/2b tokenized every in-range one).
    expect(fontSizeLiterals("  font-size: 0.8" + "rem;").map((h) => h.value)).toEqual(["0.8rem"]);
    expect(fontSizeLiterals("  font-size: 0.9" + "em;").map((h) => h.value)).toEqual(["0.9em"]);
  });

  it("the CSS matcher ignores token reads, keyword values, and a font-size inside a comment", () => {
    expect(fontSizeLiterals("  font-size: " + "var(--pl-font-size-xs);")).toEqual([]);
    expect(fontSizeLiterals("  font-size: " + "inherit;")).toEqual([]);
    // The sweep strips comments first, so a font-size shown in a comment is not a hit.
    const commented = "/* font-size: 13" + "px (legacy) */\n.x { font-size: " + "var(--pl-font-size-sm); }";
    expect(offendersIn("synthetic.css", commented)).toEqual([]);
  });

  it("the TSX matcher flags inline number and px/rem/em string literals, allows token reads", () => {
    // Flagged: an unquoted number, and quoted px/rem/em strings.
    expect(inlineFontSizeLiterals("  style={{ fontSize: " + "13 }}")).toEqual(["fontSize: 13"]);
    expect(inlineFontSizeLiterals("  fontSize: " + '"13px",')).toEqual(['fontSize: "13px"']);
    expect(inlineFontSizeLiterals("  fontSize: " + "'0.8rem',")).toEqual(["fontSize: '0.8rem'"]);
    // Allowed: a DS token read, and a non-length theme-blob value (src/lib/themeMerge `"lg"`).
    expect(inlineFontSizeLiterals("  fontSize: " + '"var(--pl-font-size-sm)",')).toEqual([]);
    expect(inlineFontSizeLiterals("  fontSize: " + '"lg",')).toEqual([]);
    // A longer identifier ending in fontSize is not an inline style key → not flagged.
    expect(inlineFontSizeLiterals("  defaultFontSize: " + "13,")).toEqual([]);
  });

  it("reports src/<path>:<line> <declaration> for a real offender (meta-guard, built by concat)", () => {
    const bad = ".x {\n  font-size: 12" + "px;\n}";
    expect(offendersIn("synthetic.css", bad)).toEqual(["synthetic.css:2 font-size: 12px"]);
    const badVar = ":root {\n  --diffs-font-size: 12" + "px;\n}";
    expect(offendersIn("synthetic.css", badVar)).toEqual(["synthetic.css:2 --diffs-font-size: 12px"]);
    const badTsx = "const s = {\n  fontSize: " + '"0.8rem",\n};';
    expect(tsxOffendersIn("../foo/Bar.tsx", badTsx)).toEqual(['src/foo/Bar.tsx:2 fontSize: "0.8rem"']);
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

  it("pins the chat.css slash-cmd exemption to selector + value (0.9em inline code chip, #1529)", () => {
    // The sanctioned inline-code chip is exempt …
    expect(isExempt(CHAT, ".chat-user-text.chat-slash-cmd", "0.9em")).toBe(true);
    // … but any other value / selector in chat.css, or a 0.9em elsewhere, still fails.
    expect(isExempt(CHAT, ".chat-user-text.chat-slash-cmd", "1.2em")).toBe(false);
    expect(isExempt(CHAT, ".chat-user-text", "0.9em")).toBe(false);
    expect(isExempt(DEVICES, ".chat-user-text.chat-slash-cmd", "0.9em")).toBe(false);
    // Proven end to end: the chip clears, a stray em under another selector is reported.
    const chatLike =
      ".chat-user-text.chat-slash-cmd {\n  font-family: var(--pl-font-mono);\n  font-size: 0.9" + "em;\n}\n" +
      ".chat-other {\n  font-size: 1.5" + "em;\n}";
    expect(offendersIn(CHAT, chatLike)).toEqual(["src/chat/chat.css:6 font-size: 1.5em"]);
  });

  it("covers the tree and every exempt file is actually in the swept glob", () => {
    const cssFiles = Object.keys(CSS_SOURCES);
    // A glob typo that silently matched nothing would fail here instead of passing vacuously.
    expect(cssFiles.length).toBeGreaterThan(20);
    // The whole-file and pinned exemptions only matter if the files are really swept.
    expect(cssFiles).toContain(APP_CRASH);
    expect(cssFiles).toContain(DEVICES);
    expect(cssFiles).toContain(CHAT);
    // The guard is a .ts file, so neither the .css- nor the .tsx-only glob includes it.
    expect(cssFiles).not.toContain("./fontSizeGuard.test.ts");
    expect(Object.keys(TSX_SOURCES)).not.toContain("./fontSizeGuard.test.ts");
  });

  it("tracks the innermost selector across nested @media rules", () => {
    const css = "@media (max-width: 767px) {\n  .devices-code code {\n    font-size: 22" + "px;\n  }\n}";
    const selectors = enclosingSelectors(stripComments(css));
    expect(selectors[3]).toBe(".devices-code code");
  });
});
