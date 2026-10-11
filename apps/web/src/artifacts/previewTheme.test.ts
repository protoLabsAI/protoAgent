// The streamed preview carries the console's theme (#4076): the same palette, console-matched
// color-scheme, html/body colours and :where() control defaults the live artifact frame gets,
// plus the DS kit CSS inlined (never a network <link> — the preview CSP stays default-src 'none').
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildPreviewSrcdoc, previewCsp } from "./StreamingPreview";
import {
  cachedPluginKitCss,
  consoleScheme,
  inlineSafeCss,
  loadPluginKitCss,
  PREVIEW_CONTROL_DEFAULTS,
  previewThemeCss,
  resetPluginKitCssForTest,
} from "./previewTheme";

const root = document.documentElement;

beforeEach(() => {
  resetPluginKitCssForTest();
  root.style.setProperty("--pl-color-bg", "#101014");
  root.style.setProperty("--pl-color-fg", "#eeeeee");
  root.style.setProperty("--pl-color-chart-series3", "#ff8800");
  root.style.setProperty("color-scheme", "dark");
});

afterEach(() => {
  root.removeAttribute("style");
  root.removeAttribute("data-theme");
  vi.restoreAllMocks();
});

describe("previewThemeCss", () => {
  it("carries the console's --pl-* tokens, its scheme, html/body colours and the control defaults", () => {
    const css = previewThemeCss();
    expect(css).toContain("--pl-color-bg:#101014;");
    expect(css).toContain("--pl-color-fg:#eeeeee;");
    expect(css).toContain("--pl-color-chart-series3:#ff8800;"); // the shell's chart tokens too
    expect(css).toContain("color-scheme:dark");
    expect(css).toContain("html,body{margin:0;background:var(--pl-color-bg);color:var(--pl-color-fg)}");
    expect(css).toContain(PREVIEW_CONTROL_DEFAULTS);
    // Keyed on [data-theme] so it outranks the kit's :root defaults AND its OS media block.
    expect(css.startsWith(":root[data-theme]{")).toBe(true);
  });

  it("follows the CONSOLE's scheme, not the OS", () => {
    // The OS says dark…
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: (q: string) => ({ matches: q.includes("dark"), media: q }) as MediaQueryList,
    });
    // …the console renders light.
    root.style.setProperty("color-scheme", "light");
    expect(consoleScheme()).toBe("light");
    expect(previewThemeCss()).toContain("color-scheme:light");
  });

  it("never inlines a value that could break out of the rule", () => {
    root.style.setProperty("--pl-color-accent", "red}</style><script>x()</script>");
    expect(previewThemeCss()).not.toContain("<script");
  });
});

describe("the preview srcdoc", () => {
  it("stamps the scheme on <html>, inlines kit + theme, and keeps the exact CSP with one script", () => {
    const doc = buildPreviewSrcdoc("abc123", { scheme: "light", css: ":root[data-theme]{--pl-color-bg:#fff}", kitCss: ".pl-btn{}" });
    expect(doc).toContain('<html lang="en" data-theme="light">');
    expect(doc).toContain('<style id="proto-preview-kit">.pl-btn{}</style>');
    expect(doc).toContain('<style id="proto-preview-theme">:root[data-theme]{--pl-color-bg:#fff}</style>');
    expect(doc).toContain(`content="${previewCsp("abc123")}"`);
    expect(doc).not.toMatch(/<link\b/i);
    expect(doc.match(/<script\b/gi)).toHaveLength(1);
  });

  it("a hostile `<` in the kit CSS can never close the style element", () => {
    const safe = inlineSafeCss("/* </style><script>alert(1)</script> */ a{}");
    expect(safe).not.toContain("<");
    const doc = buildPreviewSrcdoc("n1", { scheme: "dark", css: "", kitCss: safe });
    expect(doc.match(/<script\b/gi)).toHaveLength(1);
  });
});

describe("loadPluginKitCss", () => {
  it("fetches the kit once, caches it inline-safe, and falls back to '' on failure", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(".pl-x{} /* a<b */"));
    expect(await loadPluginKitCss()).toBe(".pl-x{} /* a\\3c b */");
    expect(await loadPluginKitCss()).toBe(".pl-x{} /* a\\3c b */");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(String(fetchSpy.mock.calls[0][0])).toContain("/_ds/plugin-kit.css");
    expect(cachedPluginKitCss()).toContain(".pl-x{}");

    resetPluginKitCssForTest();
    fetchSpy.mockRejectedValueOnce(new Error("offline"));
    expect(await loadPluginKitCss()).toBe("");
  });
});
