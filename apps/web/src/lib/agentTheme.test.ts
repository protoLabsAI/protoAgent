import { describe, it, expect, beforeEach, beforeAll } from "vitest";

import { applyAgentTheme, persistedThemeIsForCurrentAgent, syncBrowserChrome } from "./agentTheme";

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

// #1923 — the theme-color meta must carry the theme's SURFACE background (--pl-color-bg, what
// the app shell/topbar paint), never the accent: on mobile PWA/webview the meta colors the
// status-bar/notch band above the header, and an accent there renders a broken two-tone header.
// The favicon keeps the accent (the tab's brand splash). Clearing the theme must restore the
// exact static brand chrome index.html shipped.
describe("syncBrowserChrome — theme-color = surface, favicon = accent (#1923)", () => {
  // index.html's static chrome, mirrored here (jsdom doesn't load it): the brand
  // theme-color is #9b87f2 — the brand DEFAULT for the unthemed console, distinct
  // from any agent accent — and the favicon is a real fetchable asset.
  const BRAND_THEME_COLOR = "#9b87f2";
  const BRAND_ICON = "/protolabs-icon-outline.svg";

  // jsdom lacks CSS.supports, so safeColor() normalizes through the span probe → rgb().
  const ACCENT = "#ff2266";
  const ACCENT_RGB = "rgb(255, 34, 102)";
  const SURFACE = "#101013";
  const SURFACE_RGB = "rgb(16, 16, 19)";
  const CHROME_THEME = { mode: "dark" as const, overrides: { "--pl-color-accent": ACCENT, "--pl-color-bg": SURFACE } };

  const meta = () => document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')!;
  const icon = () => document.querySelector<HTMLLinkElement>('link[rel~="icon"]')!;

  // Seed the static chrome BEFORE the first syncBrowserChrome call: the module snapshots
  // the shipped defaults once (raw attributes) and restores those exact values on clear.
  beforeAll(() => {
    const l = document.createElement("link");
    l.rel = "icon";
    l.setAttribute("href", BRAND_ICON);
    document.head.appendChild(l);
    const m = document.createElement("meta");
    m.name = "theme-color";
    m.setAttribute("content", BRAND_THEME_COLOR);
    document.head.appendChild(m);
  });

  beforeEach(() => {
    localStorage.clear();
    focusAgent("host");
    applyAgentTheme(null, { animate: false }); // back to DS defaults…
    syncBrowserChrome(); // …and restore the static chrome if a previous test themed it
  });

  it("points the theme-color meta at the surface background, NOT the accent", () => {
    applyAgentTheme(CHROME_THEME, { animate: false });
    syncBrowserChrome();
    expect(meta().getAttribute("content")).toBe(SURFACE_RGB);
    expect(meta().getAttribute("content")).not.toBe(ACCENT_RGB);
  });

  it("keeps the favicon on the accent (recolored data-URI)", () => {
    applyAgentTheme(CHROME_THEME, { animate: false });
    syncBrowserChrome();
    const href = icon().getAttribute("href") ?? "";
    expect(href.startsWith("data:image/svg+xml,")).toBe(true);
    expect(href).toContain(encodeURIComponent(ACCENT_RGB));
  });

  it("clearing the theme restores the exact static brand chrome", () => {
    applyAgentTheme(CHROME_THEME, { animate: false });
    syncBrowserChrome();
    expect(meta().getAttribute("content")).toBe(SURFACE_RGB); // themed first, so there's something to restore
    applyAgentTheme(null, { animate: false });
    syncBrowserChrome();
    expect(meta().getAttribute("content")).toBe(BRAND_THEME_COLOR);
    expect(icon().getAttribute("href")).toBe(BRAND_ICON);
  });

  it("is fail-safe per token: an accent-only theme recolors the favicon and leaves the meta alone", () => {
    // No --pl-color-bg override and no stylesheet in jsdom → the surface token doesn't
    // resolve → the meta keeps its current (brand) value instead of going blank/invalid.
    // (In a real browser the DS tokens stylesheet resolves --pl-color-bg to the
    // mode-appropriate default background, which is exactly what the header paints.)
    applyAgentTheme({ mode: "dark", overrides: { "--pl-color-accent": ACCENT } }, { animate: false });
    syncBrowserChrome();
    expect(icon().getAttribute("href")).toContain(encodeURIComponent(ACCENT_RGB));
    expect(meta().getAttribute("content")).toBe(BRAND_THEME_COLOR);
  });
});
