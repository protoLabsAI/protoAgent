import { describe, expect, it } from "vitest";

// Assert on the raw stylesheet text (same source-guard pattern as hitl-accent.test.ts /
// app/statusTokenGuard.test.ts). Vitest stubs CSS imports to empty modules by default, so
// vitest.config.ts opts all of src's CSS into processing (`test.css.include`) — that's what
// lets `?raw` return the real text instead of "".
import chatComponentCss from "./chat-component.css?raw";
import promptViewerCss from "./promptviewer.css?raw";
import toolCallsCss from "./tool-calls.css?raw";

// #3685 part c2 — DS tokens are always loaded (main.tsx imports @protolabsai/design before any
// CSS), so a `var(--pl-…, #hex)` fallback can only ever paint a wrong, dark-only colour once the
// token exists; the DS owner deleted them. The legacy `brand-*` aliases are retired to DS
// tokens too: tool-call accent TEXT reads --pl-color-accent-fg (readable on the card body in
// light mode, unlike the mid-tone --pl-color-accent), and the prompt-viewer bars/borders read
// --pl-color-accent. This guard locks all three files against a lazy re-introduction of either.

const FILES: Record<string, string> = {
  "chat-component.css": chatComponentCss,
  "tool-calls.css": toolCallsCss,
  "promptviewer.css": promptViewerCss,
};

// A hex fallback INSIDE a var(): `var(--pl-color-accent, #7c8cff)` — the exact shape deleted
// here. The `#hex` may sit further along a comma-list (`var(--x, var(--y), #hex)`), so match a
// `#` hex anywhere before the closing paren of a --pl-* var().
const HEX_FALLBACK = /var\(\s*--pl-[a-z0-9-]+\s*,[^)]*#[0-9a-fA-F]{3,8}/;

// Pull a single top-level rule's body by its exact line-start selector, so each assertion is
// scoped to its site (`.tool-link` must not match `.tool-link:hover`). Targeted rules are flat
// (no nested braces), so `[^}]*` is a safe body matcher.
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
  expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
  return match![0];
}

describe("#3685 c2 — no var(--pl-…, #hex) fallbacks, no brand-* aliases, no stray hex", () => {
  for (const [name, css] of Object.entries(FILES)) {
    it(`${name} is loaded as raw text (the guard is not silently blind)`, () => {
      expect(css.length).toBeGreaterThan(0);
    });

    it(`${name} has no var(--pl-…, #hex) fallback`, () => {
      expect(css).not.toMatch(HEX_FALLBACK);
    });

    it(`${name} has no brand-* alias`, () => {
      expect(css).not.toContain("--" + "brand-");
    });

    it(`${name} carries no colour hex literal outside comments`, () => {
      // Strip block comments (which legitimately carry `#NNNN` issue refs) then assert no `#`
      // hex remains — i.e. no NEW hex literal was introduced by this change.
      const code = css.replace(/\/\*[\s\S]*?\*\//g, "");
      expect(code).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    });

    it(`${name} keeps comments free of the glued \`*\` \`/\` minifier trap`, () => {
      // Mirror scripts/check-css-comments.mjs: a `*/` glued to identifier chars closes a
      // comment early and silently drops downstream rules from the minified bundle.
      expect(css).not.toMatch(/[A-Za-z0-9_.-]\*\/[A-Za-z0-9_.-]/);
    });
  }
});

describe("chat-component.css accent/focus/surfaces read bare DS tokens", () => {
  it("card surface uses --pl-color-border + --pl-color-bg-raised", () => {
    const chatComp = rule(chatComponentCss, ".chat-comp");
    expect(chatComp).toMatch(/border:\s*1px solid var\(--pl-color-border\)/);
    expect(chatComp).toMatch(/background:\s*var\(--pl-color-bg-raised\)/);
  });

  it("done timeline dot fills + borders with --pl-color-accent", () => {
    const done = rule(chatComponentCss, ".chat-comp-step.is-done .chat-comp-step-dot");
    expect(done).toMatch(/background:\s*var\(--pl-color-accent\)/);
    expect(done).toMatch(/border-color:\s*var\(--pl-color-accent\)/);
  });

  it("active timeline dot rings with a color-mix over bare --pl-color-accent", () => {
    const active = rule(chatComponentCss, ".chat-comp-step.is-active .chat-comp-step-dot");
    expect(active).toMatch(/color-mix\(in srgb,\s*var\(--pl-color-accent\)\s+25%/);
  });

  it("code-ref chip focus ring uses --pl-color-focus", () => {
    expect(rule(chatComponentCss, ".code-ref-chip:focus-visible")).toMatch(
      /outline:\s*2px solid var\(--pl-color-focus\)/,
    );
  });

  it("code-ref chip hover uses --pl-color-accent + --pl-color-bg-hover", () => {
    const hover = rule(chatComponentCss, ".code-ref-chip:hover");
    expect(hover).toMatch(/border-color:\s*var\(--pl-color-accent\)/);
    expect(hover).toMatch(/background:\s*var\(--pl-color-bg-hover\)/);
  });
});

describe("tool-calls.css accent TEXT reads --pl-color-accent-fg (readable in light mode)", () => {
  const TEXT_SITES: Array<[string, string]> = [
    ["chip", ".tool-chip"],
    ["link", ".tool-link"],
    ["wait head icon", ".tool-wait-head svg"],
    ["calc result", ".tool-calc strong"],
    ["editor link", ".tool-editor-link"],
  ];

  for (const [label, selector] of TEXT_SITES) {
    it(`${label} text uses --pl-color-accent-fg`, () => {
      expect(rule(toolCallsCss, selector)).toMatch(/color:\s*var\(--pl-color-accent-fg\)/);
    });
  }

  it("exactly the five text sites carry --pl-color-accent-fg", () => {
    expect(toolCallsCss.match(/var\(--pl-color-accent-fg\)/g)).toHaveLength(5);
  });

  it("the manage-button hover keeps bare --pl-color-accent for its chrome tint", () => {
    const hover = rule(toolCallsCss, ".tool-manage-btn:hover");
    expect(hover).toMatch(/color:\s*var\(--pl-color-accent\)/);
    expect(hover).toMatch(/color-mix\(in srgb,\s*var\(--pl-color-accent\)\s+12%/);
  });
});

describe("promptviewer.css bars/borders read bare --pl-color-accent", () => {
  const FILL_SITES: Array<[string, string, RegExp]> = [
    ["context fill", ".prompt-viewer__budget-fill--context", /background:\s*var\(--pl-color-accent\)/],
    ["delivery fill", ".prompt-viewer__budget-fill--delivery", /background:\s*var\(--pl-color-accent\)/],
    [
      "projected fill",
      ".prompt-viewer__budget-fill--projected",
      /background:\s*var\(--pl-color-accent\)/,
    ],
    [
      "speculative border",
      ".prompt-viewer__speculative",
      /border-left:\s*2px solid var\(--pl-color-accent\)/,
    ],
  ];

  for (const [label, selector, pattern] of FILL_SITES) {
    it(`${label} uses --pl-color-accent`, () => {
      expect(rule(promptViewerCss, selector)).toMatch(pattern);
    });
  }

  it("the projected fill drops its old brand-violet var() fallback", () => {
    expect(rule(promptViewerCss, ".prompt-viewer__budget-fill--projected")).not.toContain(
      "--" + "brand-violet",
    );
  });
});
