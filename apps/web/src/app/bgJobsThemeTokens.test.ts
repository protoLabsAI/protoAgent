import { describe, expect, it } from "vitest";

// #3684 part 3b — the Background-agents chrome finished its move to DS Button/Accordion, so
// theme.css's `.bg-jobs-*` block is now pure content styling: the DS Accordion owns the row
// border/radius/background, and the per-row control chrome (rowmain/rowhead/stop/clear) is
// gone with the old markup. This guard locks that state in — no resurrected dead selectors,
// no `var(--pl-…, #hex)` fallbacks (DS tokens are always loaded, so a fallback only ever
// paints a wrong dark-only colour), and no literal px radius (the one remaining rounded
// element reads the DS `--pl-radius` token). Same source-guard shape as
// app/statusTokenGuard.test.ts / chat/chat-css-tokens.test.ts: assert on the raw stylesheet
// text (vitest.config.ts `test.css.include` opts src's CSS in so `?raw` returns real text).
import themeCss from "./theme.css?raw";
import bgJobsSource from "./BackgroundJobs.tsx?raw";

// All src CSS, for the issue-level sweep (#3684): App chrome must no longer re-implement DS
// components. Vite `?raw` globs (compile-time, rooted here) rather than node:fs — this tsconfig
// has no node types and jsdom's `import.meta.url` is an http: URL.
const CSS_SOURCES = import.meta.glob("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Strip block comments first: they legitimately carry `#NNNN` issue refs (e.g. `#2352`) that
// would trip the hex sweep, and comment text before a `{` would confuse the rule parser.
const code = themeCss.replace(/\/\*[\s\S]*?\*\//g, "");

// Pull every flat rule whose selector mentions `.bg-jobs` (the block is comment-free after the
// strip and has no nested braces, so `[^{}]` bodies are safe).
function bgJobsRules(css: string): Array<{ selector: string; body: string }> {
  const rules: Array<{ selector: string; body: string }> = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css)) !== null) {
    const selector = m[1].trim();
    if (selector.includes(".bg-jobs")) rules.push({ selector, body: m[2] });
  }
  return rules;
}

describe("#3684 3b — theme.css .bg-jobs block is DS-token-only content styling", () => {
  it("theme.css is loaded as raw text (the guard is not silently blind)", () => {
    expect(themeCss.length).toBeGreaterThan(0);
    expect(bgJobsRules(code).length).toBeGreaterThan(0);
  });

  it("drops the control chrome the DS Accordion/Button now owns", () => {
    // The whole-word row rule (`\.bg-jobs-row {`, not the deleted `-rowmain`/`-rowhead`) and the
    // per-row buttons/detail wrapper are gone; the row's border/radius/background come from
    // `.pl-accordion` now.
    expect(code).not.toMatch(/\.bg-jobs-row\s*[,{]/);
    for (const dead of ["rowmain", "rowhead", "stop", "clear", "detail"]) {
      expect(code, `.bg-jobs-${dead} must not resurface`).not.toContain(`.bg-jobs-${dead}`);
    }
  });

  it("keeps the still-rendered rules the markup and e2e depend on", () => {
    const selectors = bgJobsRules(code).map((r) => r.selector);
    for (const kept of [".bg-jobs-unread", ".bg-jobs-result", ".bg-jobs-toolbar", ".bg-jobs-feed"]) {
      expect(selectors.some((s) => s.split(/[\s,+>]/).includes(kept))).toBe(true);
    }
  });

  it("carries no var(--pl-…, #hex) fallback and no bare hex in the block", () => {
    for (const { selector, body } of bgJobsRules(code)) {
      expect(body, `${selector} still has a #hex literal`).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    }
  });

  it("uses no literal px radius — the DS --pl-radius token instead", () => {
    for (const { selector, body } of bgJobsRules(code)) {
      const radius = /border-radius:\s*([^;]+)/.exec(body);
      if (radius) {
        expect(radius[1], `${selector} border-radius must be a DS token`).not.toMatch(/\d+px/);
      }
    }
    // The unread badge dot is the one rounded element left; it must read the token.
    const unread = bgJobsRules(code).find((r) => r.selector === ".bg-jobs-unread");
    expect(unread?.body).toMatch(/border-radius:\s*var\(--pl-radius\)/);
  });
});

describe("#3684 — App chrome no longer re-implements DS components (issue-level sweep)", () => {
  it("no src stylesheet is stubbed empty (widen test.css.include otherwise)", () => {
    expect(Object.keys(CSS_SOURCES).length).toBeGreaterThan(5);
    for (const [file, text] of Object.entries(CSS_SOURCES)) {
      expect(text.length, `${file} imported empty`).toBeGreaterThan(0);
    }
  });

  it("no .status-dot, .util-btn, DS-Drawer sheet chrome, or bg-jobs control chrome remains", () => {
    const banned = [
      ".status-dot",
      ".util-btn",
      ".session-sheet-root",
      ".session-sheet-backdrop",
      ".session-sheet-grip",
      ".bg-jobs-rowmain",
      ".bg-jobs-rowhead",
      ".bg-jobs-stop",
      ".bg-jobs-clear",
    ];
    for (const [file, text] of Object.entries(CSS_SOURCES)) {
      for (const cls of banned) {
        expect(text, `${file} still defines ${cls}`).not.toContain(cls);
      }
    }
  });
});

describe("#3684 3b — BackgroundJobs.tsx still renders the e2e-load-bearing hooks", () => {
  it("keeps bg-jobs-row, bg-jobs-result and bg-jobs-unread classNames", () => {
    for (const cls of ["bg-jobs-row", "bg-jobs-result", "bg-jobs-unread"]) {
      expect(bgJobsSource, `BackgroundJobs.tsx must still render .${cls}`).toContain(`"${cls}"`);
    }
  });

  it("drops the orphan bg-jobs-actions className (no rule, no test meaning)", () => {
    expect(bgJobsSource).not.toContain("bg-jobs-actions");
  });
});
