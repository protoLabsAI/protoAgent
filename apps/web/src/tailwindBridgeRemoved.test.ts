import { describe, expect, it } from "vitest";

// Regression guard for #3686 (part 1): ADR 0037's incremental migration left a dead
// shadcn/Tailwind CSS bridge in the console (app/tailwind.css + tailwind.config.cjs). Nothing
// consumed it — no Tailwind utility classNames, no @apply, preflight already off — so the
// wiring was removed. This sweep keeps it from creeping back: the tailwind.css entrypoint stays
// gone, main.tsx never re-imports it, and no source references a shadcn color/radius token
// (var(--background|foreground|...|radius)); every color must resolve through the --pl-* tokens.
//
// Vite `?raw` globs rather than node:fs — mirrors statusTokenGuard.test.ts: this tsconfig has
// no node types, and under jsdom `import.meta.url` is an http: URL, so URL-relative filesystem
// access is a trap. The globs are compile-time, rooted at this file (src/), and pick up new
// source files automatically.
const TS_SOURCES = import.meta.glob("./**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;
const CSS_SOURCES = import.meta.glob("./**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// The shadcn color/radius convention this card retired. Mirrors the card's hazard-check grep:
// var(--<token>) with a word boundary, so a fallback form (var(--border, #ccc)) is caught too.
// The pattern is self-non-matching — after `var\(--` its own source has `(`, not a token — so
// this test file can carry it without tripping its own sweep.
const SHADCN =
  /var\(--(background|foreground|card|popover|primary|secondary|muted|accent|destructive|border|input|ring|radius)\b/;

function offenders(sources: Record<string, string>): string[] {
  const hits: string[] = [];
  for (const [file, text] of Object.entries(sources)) {
    text.split("\n").forEach((line, i) => {
      const pretty = file.replace(/^\.\//, "src/");
      if (SHADCN.test(line)) hits.push(`${pretty}:${i + 1}`);
    });
  }
  return hits;
}

describe("shadcn/Tailwind CSS bridge stays removed (#3686 part 1)", () => {
  const MAIN = TS_SOURCES["./main.tsx"];

  it("main.tsx is present in the sweep (glob actually resolved it)", () => {
    expect(typeof MAIN).toBe("string");
    expect(MAIN.length).toBeGreaterThan(0);
  });

  it("main.tsx no longer imports the tailwind.css entrypoint", () => {
    expect(MAIN).not.toMatch(/tailwind\.css/);
  });

  it("main.tsx's load-order comment no longer names Tailwind or the shadcn bridge", () => {
    expect(MAIN).not.toMatch(/tailwind/i);
    expect(MAIN).not.toMatch(/shadcn/i);
  });

  it("main.tsx still loads the DS token + component styles in order (tokens first)", () => {
    // The card is scoped to dropping the Tailwind line only — the surrounding DS imports and
    // their order are preserved. Tokens must precede the DS component styles.
    const tokensAt = MAIN.indexOf("@protolabsai/design/css/tokens");
    const stylesAt = MAIN.indexOf("@protolabsai/ui/styles.css");
    expect(tokensAt).toBeGreaterThanOrEqual(0);
    expect(stylesAt).toBeGreaterThan(tokensAt);
  });

  it("the tailwind.css entrypoint is gone from the tree", () => {
    const tailwindEntries = Object.keys(CSS_SOURCES).filter((k) => /tailwind\.css$/.test(k));
    expect(tailwindEntries).toEqual([]);
  });

  it("css: no stylesheet references a shadcn color/radius token", () => {
    expect(offenders(CSS_SOURCES)).toEqual([]);
  });

  it("ts/tsx: no component or test references a shadcn color/radius token", () => {
    expect(offenders(TS_SOURCES)).toEqual([]);
  });

  it("sweeps the real stylesheet text — a stubbed (empty) css import blinds the guard", () => {
    // Vitest stubs css imports to "" unless vitest.config.ts `test.css.include` opts the file
    // in; the include covers all of src. This keeps the sweep from silently passing on "".
    for (const [file, text] of Object.entries(CSS_SOURCES)) {
      expect(text.length, `${file} imported empty — widen test.css.include in vitest.config.ts`).toBeGreaterThan(0);
    }
  });

  it("actually covers the tree (floors, so file moves don't churn the test)", () => {
    expect(Object.keys(CSS_SOURCES).length).toBeGreaterThan(20);
    expect(Object.keys(TS_SOURCES).length).toBeGreaterThan(100);
  });

  it("the pattern itself still bites (meta-guard, literals built by concat so this file stays clean)", () => {
    const V = "var(" + "--";
    expect(SHADCN.test(V + "border)")).toBe(true);
    expect(SHADCN.test(V + "primary, #ccc)")).toBe(true);
    expect(SHADCN.test(V + "radius)")).toBe(true);
    // The brand tokens the console actually uses stay clean — the token isn't right after `--`.
    expect(SHADCN.test(V + "pl-color-border)")).toBe(false);
    expect(SHADCN.test(V + "pl-radius)")).toBe(false);
  });
});
