import { describe, expect, it } from "vitest";

// #3682 (part 1/3): four console surfaces read CSS custom properties the pinned
// @protolabsai/design (^0.9.2) never defines — --pl-color-text-muted (memory, chat),
// --pl-color-{success,error} (workflows) and the nested --pl-bg/--pl-fg fallback
// (app-crash). A phantom var() never resolves, so every site painted its hardcoded
// dark hex fallback and stayed deaf to light mode + ThemePanel overrides. This pins the
// re-point onto the real DS tokens (--pl-color-fg-muted / --pl-color-status-{success,error}
// / --pl-color-{bg,fg}) with the original fallbacks intact.
//
// Source-level (Vite ?raw) rather than a rendered getComputedStyle assertion, by design:
// the DS ships from a private registry and isn't in node_modules here, so the real token
// resolves to nothing at runtime and only the fallback is ever observable — a rendered
// test could not prove the light token (#52525b for --pl-color-fg-muted) now wins. The
// rename IS the fix, so the rename is what we pin. Same ?raw approach, and same reason
// (no node types on this tsconfig), as statusTokenGuard.test.ts.

const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/app): siblings key as
// `../dir/name`, same-dir files as `./name`. Match by path suffix so a file move
// doesn't churn the test.
function source(suffix: string): string {
  const hit = Object.entries(CSS_SOURCES).find(([file]) => file.endsWith(suffix));
  if (!hit) throw new Error(`stylesheet not found in ?raw glob: ${suffix}`);
  return hit[1];
}

const count = (text: string, needle: string): number => text.split(needle).length - 1;

// The retired phantom names. --pl-color-status-{success,error} are the REAL tokens and
// must not trip the success/error rules, so those carry a negative lookahead that also
// rejects the `status-` infix; --pl-(bg|fg) must not match --pl-color-bg / --pl-color-fg
// / --pl-font, which it can't since it demands `bg`/`fg` immediately after `--pl-`.
const PHANTOM =
  /--pl-color-text-muted|--pl-color-success(?![\w-])|--pl-color-error(?![\w-])|--pl-(?:bg|fg)(?![\w-])/;

function offenders(sources: Record<string, string>): string[] {
  const hits: string[] = [];
  for (const [file, text] of Object.entries(sources)) {
    const pretty = file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/app/");
    text.split("\n").forEach((line, i) => {
      if (PHANTOM.test(line)) hits.push(`${pretty}:${i + 1}`);
    });
  }
  return hits;
}

describe("phantom CSS tokens re-pointed to real DS tokens (#3682)", () => {
  it("memory.css: every muted-text site reads --pl-color-fg-muted; the #8a8f98 fallback was stripped in #3685 (d2)", () => {
    const mem = source("/memory/memory.css");
    // #3682's rename target still holds — no phantom --pl-color-text-muted survives.
    expect(mem).not.toContain("--pl-color-text-muted");
    // #3685 (d2) then deleted the dark-only hex fallbacks: the seven renamed sites plus the
    // pre-existing .memory-injections-context site now all read the bare real token.
    expect(count(mem, "var(--pl-color-fg-muted, #8a8f98)")).toBe(0);
    expect(count(mem, "var(--pl-color-fg-muted)")).toBe(8);
  });

  it("chat.css: the model-lane label reads --pl-color-fg-muted with its #8b8b93 fallback", () => {
    expect(count(source("/chat/chat.css"), "var(--pl-color-fg-muted, #8b8b93)")).toBe(1);
  });

  it("workflows.css: the ok dot reads --pl-color-status-success, the err dot + bad toolchip --pl-color-status-error, fallbacks intact", () => {
    const wf = source("/workflows/workflows.css");
    expect(count(wf, "var(--pl-color-status-success, #57b880)")).toBe(1);
    expect(count(wf, "var(--pl-color-status-error, #d8635b)")).toBe(2);
  });

  it("app-crash.css: bg/fg read the real tokens with a single flat fallback, no nested --pl-bg/--pl-fg", () => {
    const crash = source("/app-crash.css");
    expect(crash).toContain("background: var(--pl-color-bg, #0a0a0c);");
    expect(crash).toContain("color: var(--pl-color-fg, #ededed);");
    expect(crash).not.toContain("--pl-bg");
    expect(crash).not.toContain("--pl-fg");
  });

  it("no phantom name survives in any console stylesheet", () => {
    expect(offenders(CSS_SOURCES)).toEqual([]);
  });

  it("sweeps real stylesheet text — a stubbed (empty) css import would blind the sweep", () => {
    // Guarded by vitest.config.ts `test.css.include`; if that regresses, the ?raw import
    // returns "" and the sweep above would pass on nothing. Floor the tree size too so a
    // glob typo that matches nothing fails loudly instead of sweeping an empty set.
    for (const [file, text] of Object.entries(CSS_SOURCES)) {
      expect(text.length, `${file} imported empty — widen test.css.include`).toBeGreaterThan(0);
    }
    expect(Object.keys(CSS_SOURCES).length).toBeGreaterThan(20);
  });

  it("the phantom pattern still bites (meta-guard, literals built by concat so this file never self-flags)", () => {
    const p = "--pl-color-";
    expect(PHANTOM.test("color: var(" + p + "text-muted, #8a8f98);")).toBe(true);
    expect(PHANTOM.test("background: var(" + p + "success, #57b880);")).toBe(true);
    expect(PHANTOM.test("border-color: var(" + p + "error, #d8635b);")).toBe(true);
    expect(PHANTOM.test("background: var(" + "--pl-" + "bg, #0a0a0c);")).toBe(true);
    expect(PHANTOM.test("color: var(" + "--pl-" + "fg, #ededed);")).toBe(true);
    // The real replacements and unrelated tokens stay clean.
    expect(PHANTOM.test("color: var(--pl-color-fg-muted, #8a8f98);")).toBe(false);
    expect(PHANTOM.test("background: var(--pl-color-status-success, #57b880);")).toBe(false);
    expect(PHANTOM.test("border-color: var(--pl-color-status-error, #d8635b);")).toBe(false);
    expect(PHANTOM.test("background: var(--pl-color-bg, #0a0a0c);")).toBe(false);
    expect(PHANTOM.test("color: var(--pl-color-fg, #ededed);")).toBe(false);
    expect(PHANTOM.test("font-family: var(--pl-font-mono);")).toBe(false);
    // --pl-color-text (without -muted) is a different, out-of-scope token; left untouched.
    expect(PHANTOM.test("color: var(--pl-color-text, inherit);")).toBe(false);
  });
});
