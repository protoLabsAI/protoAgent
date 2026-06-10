// Build guard against a corruption class that has bitten the console CSS three
// times (#863 + the chat/theme comments fixed alongside this script): a stray
// `*/` *inside* a CSS comment — almost always a glob written in prose, e.g.
//
//     /* the .plugin-install-*/.plugin-list family moved to plugins.css */
//                            ^^ this closes the comment EARLY
//
// Everything after the premature `*/` is then parsed as CSS. esbuild's minifier
// emits a "css-syntax-error" WARNING (not an error) and recovers by dropping
// tokens — so a real rule downstream can silently vanish from dist (this is the
// root cause of the "tiny plugin iframes" bug: a dropped `.plugin-view` rule fell
// back to the stage-panel grid). The build still "succeeds", so nobody notices.
//
// The signature is unmistakable and has no legitimate counterpart: a `*/` glued
// to identifier-ish characters on BOTH sides. A real comment terminator is always
// ` */` (whitespace/newline around it), and `*/` cannot appear outside a comment
// in valid CSS at all. So any match here is a bug — we fail the build loudly.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, "..", "src");

// `*/` with identifier chars (incl. `.` `-` `_`) immediately on each side.
const GLUED_COMMENT_CLOSE = /[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/;

function cssFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...cssFiles(p));
    else if (name.endsWith(".css")) out.push(p);
  }
  return out;
}

const violations = [];
for (const file of cssFiles(SRC)) {
  const lines = readFileSync(file, "utf8").split("\n");
  lines.forEach((line, i) => {
    const m = GLUED_COMMENT_CLOSE.exec(line);
    if (m) violations.push({ file: relative(SRC, file), line: i + 1, snippet: line.trim() });
  });
}

if (violations.length) {
  console.error(
    "\n[check-css-comments] A `*/` is glued inside a CSS comment — it closes the\n" +
      "comment early and silently corrupts everything after it in the minified bundle.\n" +
      "Put a space around the slash (`.foo* / .bar`) or reword the prose.\n",
  );
  for (const v of violations) {
    console.error(`  src/${v.file}:${v.line}\n    ${v.snippet}`);
  }
  console.error("");
  process.exit(1);
}

console.log("[check-css-comments] OK — no glued */ in any src CSS comment.");
