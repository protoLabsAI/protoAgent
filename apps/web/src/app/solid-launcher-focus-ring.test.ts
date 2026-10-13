import { describe, expect, it } from "vitest";

// Assert on the raw stylesheet text (same source-guard pattern as
// chat/hitl-accent.test.ts / app/statusTokenGuard.test.ts). Vitest stubs CSS imports to empty
// modules by default; `vitest.config.ts` opts all of `src/**/*.css` into processing so `?raw`
// returns the real text.
import launcherCss from "./launcher.css?raw";
import themeBaseCss from "./theme-base.css?raw";
import fleetCss from "../fleet/fleet.css?raw";

// #3687 — two brand-rule breaks fixed as a pure consumer change: the desktop launcher panel
// drops its glass-morphism (translucent surface + backdrop blur — banned on UI surfaces by
// the DS ruling) for a solid DS raised surface, and the global focus ring goes from
// `1px solid --pl-color-fg` to the DS-standard `2px solid --pl-color-focus`.

// Pull a single top-level rule's body by its exact line-start selector, so each assertion is
// scoped to its site.
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${escaped}\\s*\\{[^}]*\\}`, "m").exec(css);
  expect(match, `expected a \`${selector}\` rule`).not.toBeNull();
  return match![0];
}

describe("launcher panel is a solid DS raised surface — no glass morphism (#3687)", () => {
  it("no backdrop-filter anywhere in launcher.css", () => {
    expect(launcherCss).not.toMatch(/backdrop-filter/);
  });

  it("panel background is the solid raised token — no color-mix translucency", () => {
    const panel = rule(launcherCss, ".is-launcher .pl-cmdk__panel");
    expect(panel).toMatch(/background:\s*var\(--pl-color-bg-raised\);/);
    expect(panel).not.toMatch(/color-mix/);
  });

  it("panel shadow is exactly the DS popover shadow — no extra drop layer", () => {
    const panel = rule(launcherCss, ".is-launcher .pl-cmdk__panel");
    expect(panel).toMatch(/box-shadow:\s*var\(--pl-shadow-popover\);/);
    // The old `0 24px 60px -20px rgb(...)` second layer is gone.
    expect(panel).not.toMatch(/box-shadow:[^;]*,/);
  });

  it("panel comment no longer describes translucency or blur", () => {
    // Grab the SINGLE comment block immediately preceding the panel rule — `(?!\*\/)` bars a
    // `*/` inside, so this can't reach back to the file-header comment.
    const match = /\/\*(?:(?!\*\/)[^])*\*\/\s*\.is-launcher \.pl-cmdk__panel/.exec(launcherCss);
    expect(match, "expected a comment above the panel rule").not.toBeNull();
    const comment = match![0];
    expect(comment).not.toMatch(/translucen|backdrop|blur|frost/i);
  });

  it("keeps the transparent overlay scrim unchanged (see-through window margin is intended)", () => {
    const overlay = rule(launcherCss, ".is-launcher .pl-cmdk-overlay");
    expect(overlay).toMatch(/background:\s*transparent\s*!important;/);
  });
});

describe("global focus ring is 2px solid --pl-color-focus (#3687)", () => {
  it("theme-base button/input/textarea/select focus-visible", () => {
    const focus = rule(
      themeBaseCss,
      "button:focus-visible,\ninput:focus-visible,\ntextarea:focus-visible,\nselect:focus-visible",
    );
    expect(focus).toMatch(/outline:\s*2px solid var\(--pl-color-focus\);/);
    expect(focus).toMatch(/outline-offset:\s*2px;/);
    // The old 1px --pl-color-fg ring is gone from this rule.
    expect(focus).not.toMatch(/outline:\s*1px solid var\(--pl-color-fg\)/);
  });

  it("fleet-name-link focus-visible mirrors the theme-base rule", () => {
    const link = rule(fleetCss, ".fleet-name-link:focus-visible");
    expect(link).toMatch(/outline:\s*2px solid var\(--pl-color-focus\);/);
    expect(link).toMatch(/outline-offset:\s*2px;/);
  });
});
