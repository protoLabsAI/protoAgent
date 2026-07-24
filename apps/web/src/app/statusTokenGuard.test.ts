import { describe, expect, it } from "vitest";

// Repo-wide source guard for #2224: the design package ships --pl-color-status-{info,
// warning,error,success}, but six sites had drifted onto phantom --pl-color-{info,warning,
// danger} names the DS never defines — so their hex fallbacks rendered permanently, deaf to
// operator theme overrides and light mode. The sites are re-pointed; this sweep keeps the
// phantom names from coming back anywhere in the console source.
//
// Vite `?raw` globs rather than node:fs — this tsconfig has no node types (see
// devicesBind.test.ts), and under jsdom `import.meta.url` is an http: URL, so
// URL-relative filesystem access is a trap. The globs are compile-time, rooted at this
// file, and pick up new source files automatically.
const TS_SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// theme-base.css is the one sanctioned bridge (#832 Axis A): the :root block aliasing
// legacy app vars onto real --pl-* tokens. If a compat alias for a phantom name is ever
// needed, it lives there — every other file must reference the real --pl-color-status-*.
// Glob keys are importer-relative: same-directory files key as `./name`, siblings as
// `../dir/name` — this file lives in src/app beside the bridge.
const BRIDGE = "./theme-base.css";

// Matches a bare phantom token but not the real --pl-color-status-* names (those have
// `status-` between the prefix and the tone) and not longer legit tokens (the lookahead).
// Built so this file never contains a bare phantom literal and can't flag itself.
const PHANTOM = /--pl-color-(info|warning|danger)(?![\w-])/;

function offenders(sources: Record<string, string>): string[] {
  const hits: string[] = [];
  for (const [file, text] of Object.entries(sources)) {
    if (file === BRIDGE) continue;
    text.split("\n").forEach((line, i) => {
      const pretty = file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");
      if (PHANTOM.test(line)) hits.push(`${pretty}:${i + 1}`);
    });
  }
  return hits;
}

describe("no phantom status tokens outside the theme-base.css bridge (#2224)", () => {
  it("css: every stylesheet references --pl-color-status-*, never a phantom name", () => {
    expect(offenders(CSS_SOURCES)).toEqual([]);
  });

  it("ts/tsx: no phantom names in components or tests (the old hitl-accent pins)", () => {
    expect(offenders(TS_SOURCES)).toEqual([]);
  });

  it("sweeps the real stylesheet text — a stubbed (empty) css import blinds the guard", () => {
    // Vitest stubs css imports to "" unless vitest.config.ts `test.css.include` opts the
    // file in; the include covers all of src, and this keeps it that way.
    for (const [file, text] of Object.entries(CSS_SOURCES)) {
      expect(text.length, `${file} imported empty — widen test.css.include in vitest.config.ts`).toBeGreaterThan(0);
    }
  });

  it("actually covers the tree, including the six fixed sites' files", () => {
    const cssFiles = Object.keys(CSS_SOURCES);
    const tsFiles = Object.keys(TS_SOURCES);
    for (const known of ["./theme.css", "../chat/chat.css", "../settings/keybindings.css", BRIDGE]) {
      expect(cssFiles).toContain(known);
    }
    // Floors, not exact counts, so file moves don't churn this test — but a glob typo
    // that silently matches nothing fails here instead of passing an empty sweep.
    expect(cssFiles.length).toBeGreaterThan(20);
    expect(tsFiles.length).toBeGreaterThan(100);
  });

  it("the pattern itself still bites (meta-guard, phantom literals built by concat)", () => {
    expect(PHANTOM.test("color: var(" + "--pl-color-" + "danger, #e5484d);")).toBe(true);
    expect(PHANTOM.test("border-left-color: var(" + "--pl-color-" + "warning);")).toBe(true);
    expect(PHANTOM.test("var(" + "--pl-color-" + "info, #3b82f6)")).toBe(true);
    // The real tokens and longer names stay clean.
    expect(PHANTOM.test("color: var(--pl-color-status-warning);")).toBe(false);
    expect(PHANTOM.test("color: var(--pl-color-status-info);")).toBe(false);
    expect(PHANTOM.test("var(" + "--pl-color-" + "information)")).toBe(false);
  });

  it("the bridge aliases legacy status vars onto the real tokens", () => {
    const bridge = CSS_SOURCES[BRIDGE];
    expect(bridge).toMatch(/--warning:\s*var\(--pl-color-status-warning/);
    expect(bridge).toMatch(/--danger:\s*var\(--pl-color-status-error/);
    expect(bridge).toMatch(/--info:\s*var\(--pl-color-status-info/);
  });
});
