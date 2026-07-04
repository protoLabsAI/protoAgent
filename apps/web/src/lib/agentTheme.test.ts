import { describe, it, expect, beforeEach } from "vitest";

import { applyAgentTheme, persistedThemeIsForCurrentAgent } from "./agentTheme";

// #1762 boot-merge blocker — `pl-theme` is a single GLOBAL localStorage key shared by every
// same-origin agent window (the fleet console is slug-routed on one origin, ADR 0042). So the
// persisted blob may belong to a DIFFERENT agent. persistedThemeIsForCurrentAgent() is the guard
// useActiveTheme uses to decide whether merging the persisted working copy over the incoming
// server default is safe (this agent's unsaved tweak) or would bleed the wrong agent's theme.

const THEME = { mode: "dark" as const, overrides: { "--pl-color-accent": "#9b87f2" } };

function focusAgent(slug: string) {
  // currentSlug() reads /agent/<slug>/ from the path; anything else is the "host" console.
  const path = slug === "host" ? "/app/" : `/app/agent/${slug}/`;
  window.history.pushState({}, "", path);
}

describe("persistedThemeIsForCurrentAgent — cross-agent boot-merge guard (#1762)", () => {
  beforeEach(() => {
    localStorage.clear();
    focusAgent("host");
  });

  it("is false when nothing has stamped an owner yet (fresh / pre-fix localStorage)", () => {
    localStorage.setItem("pl-theme", JSON.stringify(THEME)); // a blob with no owner stamp
    expect(persistedThemeIsForCurrentAgent()).toBe(false);
  });

  it("is true for the agent whose applyAgentTheme wrote the blob", () => {
    focusAgent("alpha");
    applyAgentTheme(THEME, { animate: false });
    expect(persistedThemeIsForCurrentAgent()).toBe(true);
  });

  it("is false from another agent's window — a different agent's theme must not be merged in", () => {
    focusAgent("alpha");
    applyAgentTheme(THEME, { animate: false }); // alpha stamps ownership on the shared key
    focusAgent("beta"); // beta's window sees alpha's blob still sitting in localStorage
    expect(persistedThemeIsForCurrentAgent()).toBe(false);
  });

  it("clears the owner stamp on reset so the guard goes false", () => {
    focusAgent("alpha");
    applyAgentTheme(THEME, { animate: false });
    expect(persistedThemeIsForCurrentAgent()).toBe(true);
    applyAgentTheme(null, { animate: false }); // reset to design-system defaults
    expect(persistedThemeIsForCurrentAgent()).toBe(false);
  });
});
