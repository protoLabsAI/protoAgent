import { describe, expect, it } from "vitest";

// DS 0.63 card 3h (final): cards 3a–3f moved every content <Dialog> to padding="roomy"
// (= `.pl-dialog__body--roomy`, var(--pl-space-6) = 24px) and card 3g moved the three
// edge-to-edge dialogs to padding="none" (= `.pl-dialog__body--flush`, 0). With the body
// inset now sourced from the DS `padding` prop, the app-wide
// `.pl-dialog__body { padding: --pl-space-6 }` default in theme.css and the `padding: 0`
// counter-overrides in settings.css (`.settings-overlay`/`.theme-quick-dialog`) and
// goals.css (`.goal-create-modal`) are redundant, so this card deletes them.
//
// This guard pins the deletion: NO `.pl-dialog__body` rule under apps/web/src may declare
// padding again. A reintroduced app-wide default would double-apply over the DS `--roomy`
// class; a reintroduced counter-override would re-couple the flush dialogs to a rule that no
// longer exists. The remaining `.pl-dialog__body` rules (mcp-catalog-dialog flex,
// archetype-setup-dialog color, doc-viewer flex/overflow) are non-padding and stay.
//
// Source-level (Vite ?raw), same rationale as dsTokenFallbackStrip.test.ts: the DS ships
// from a private registry and isn't in node_modules here, so getComputedStyle can't resolve
// the DS `--pl-space-6` token at runtime — the deletion IS the change, so the CSS text is
// what we pin. The glob is compile-time and rooted at this file (src/app), so a file move is
// matched by suffix rather than churned.

const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Strip CSS comments first — settings.css's block comment mentions `.pl-dialog__body … padding:0`
// while explaining the (now-removed) rule, and that prose must not be mistaken for a declaration.
const stripComments = (css: string): string => css.replace(/\/\*[\s\S]*?\*\//g, "");

// Every rule whose selector targets the dialog BODY element itself (`.pl-dialog__body`), NOT a
// modifier class (`.pl-dialog__body--roomy`/`--flush`, which the DS owns and which legitimately
// set padding). `(?![\w-])` rejects the `--` modifier suffix. Captures the declaration block.
const BODY_RULE = /\.pl-dialog__body(?![\w-])[^{}]*\{([^}]*)\}/g;

// A rule whose selector is EXACTLY `.pl-dialog__body` (unscoped) — the deleted app-wide default.
// The selector must start fresh (file start, or after a prior rule's `}`), so a scoped variant
// like `.settings-overlay > .pl-dialog__body {` (preceded by `> `) does not match.
const UNSCOPED_RULE = /(?:^|[}])\s*\.pl-dialog__body\s*\{/m;

function bodyRules(css: string): string[] {
  const stripped = stripComments(css);
  return [...stripped.matchAll(BODY_RULE)].map((m) => m[1]);
}

describe("app-wide/counter-override .pl-dialog__body padding removed (#3688 card 3h)", () => {
  it("globbed the real stylesheets, not empty stubs", () => {
    const entries = Object.entries(CSS_SOURCES);
    expect(entries.length, "no CSS matched the ?raw glob").toBeGreaterThan(0);
    // theme.css/settings.css/goals.css must be present or the guard passes vacuously.
    // Glob keys are importer-relative (this file lives in src/app), so theme.css keys as
    // `./theme.css` while siblings key as `../dir/name.css`.
    for (const suffix of ["/theme.css", "/settings/settings.css", "/goals/goals.css"]) {
      const hit = entries.find(([file]) => file.endsWith(suffix));
      expect(hit, `stylesheet not found in glob: ${suffix}`).toBeTruthy();
      expect(hit![1].length, `${suffix} imported empty — check the ?raw import`).toBeGreaterThan(50);
    }
  });

  it("no .pl-dialog__body rule anywhere declares padding", () => {
    for (const [file, css] of Object.entries(CSS_SOURCES)) {
      for (const block of bodyRules(css)) {
        expect(block, `${file}: a .pl-dialog__body rule still declares padding`).not.toMatch(
          /padding/,
        );
      }
    }
  });

  it("theme.css no longer holds the unscoped app-wide .pl-dialog__body rule", () => {
    const theme = Object.entries(CSS_SOURCES).find(([f]) => f.endsWith("/theme.css"))![1];
    expect(stripComments(theme)).not.toMatch(UNSCOPED_RULE);
  });

  it("the scoped edge-to-edge dialog bodies keep their sizing but no padding", () => {
    const settings = Object.entries(CSS_SOURCES).find(([f]) =>
      f.endsWith("/settings/settings.css"),
    )![1];
    const stripped = stripComments(settings);
    // The two overlay rules survive (sizing/scroll/flex) — just without padding.
    for (const sel of [".settings-overlay > .pl-dialog__body", ".theme-quick-dialog > .pl-dialog__body"]) {
      const rule = new RegExp(
        sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}",
      ).exec(stripped);
      expect(rule, `${sel} rule should still exist`).toBeTruthy();
      expect(rule![1], `${sel} should keep its sizing declarations`).toMatch(/overflow/);
      expect(rule![1], `${sel} should no longer declare padding`).not.toMatch(/padding/);
    }
  });

  it("the extractor bites (meta-guard, literals built by concat so this file can't self-flag)", () => {
    const body = ".pl-dialog__" + "body";
    const withPadding = `${body} { padding: 24px; }`;
    expect(bodyRules(withPadding)[0]).toContain("padding");
    // A `--roomy` modifier that sets padding is the DS's own and must NOT be captured.
    const modifier = `${body}--roomy { padding: 24px; }`;
    expect(bodyRules(modifier)).toHaveLength(0);
    // padding named only inside a comment is stripped, not flagged.
    const commented = `/* ${body} padding:0 */\n${body} { display: flex; }`;
    expect(bodyRules(commented)[0]).not.toContain("padding");
    // The unscoped-rule matcher rejects a scoped selector.
    expect(`.scope > ${body} { color: red; }`).not.toMatch(UNSCOPED_RULE);
    expect(`${body} { color: red; }`).toMatch(UNSCOPED_RULE);
  });
});
