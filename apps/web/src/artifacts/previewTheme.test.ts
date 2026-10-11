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

// Preview/live parity for the page box (#4154 review): the streamed preview stands in for the
// live inline frame, so html/body must get the SAME margin/padding/font-size there as the shell's
// `base()` gives the live frame — else the content shifts at handover. The live frame sets
// `html,body{margin:0}` and no padding or font-size of its own (DEFAULTS only adds font-family +
// line-height; size comes from the DS kit, inlined into both). Read from shell.js itself so a
// change on either side fails here.
const SHELL = Object.values(
  import.meta.glob("../../../../plugins/artifact/shell.js", { query: "?raw", import: "default", eager: true }),
)[0] as string;

/** The declarations every rule targeting html/body (bare or `:where()`) sets, merged in order. */
function pageBoxDecls(css: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sel = m[1].trim();
    if (sel !== "html,body" && sel !== ":where(html,body)") continue;
    for (const d of m[2].split(";")) {
      const i = d.indexOf(":");
      if (i > 0) out[d.slice(0, i).trim()] = d.slice(i + 1).trim();
    }
  }
  return out;
}

/** The live frame's html/body CSS: shell.js `DEFAULTS` plus `base()`'s html,body rule, with the
 *  JS string concatenation flattened (`'+bg+'` placeholders become `X`). */
function liveFrameCss(): string {
  const defaults = /var DEFAULTS = ([\s\S]*?);\n/.exec(SHELL)?.[1] ?? "";
  const baseRule = /\+ ('html,body\{margin:0;background:.*)\n/.exec(SHELL)?.[1] ?? "";
  const flat = (js: string) => js.replace(/'\s*\+\s*'/g, "").replace(/'\+\w+\+'/g, "X").replace(/^'|'$/g, "");
  return flat(defaults) + flat(baseRule);
}

describe("preview/live parity", () => {
  it("html/body get the same margin, padding and font-size as the live inline frame", () => {
    expect(SHELL).toContain("function base(kind)");
    const live = pageBoxDecls(liveFrameCss());
    const preview = pageBoxDecls(previewThemeCss());
    expect(live.margin).toBe("0"); // the extraction found the live rule at all
    for (const prop of ["margin", "padding", "font-size", "font"]) {
      expect(preview[prop], prop).toBe(live[prop]);
    }
    // Pinned explicitly too: no padding, no font-size override in either (the old preview's
    // `padding:8px;font:14px/1.5 …` was the drift this guards against).
    expect(preview.padding).toBeUndefined();
    expect(preview["font-size"]).toBeUndefined();
    expect(preview.font).toBeUndefined();
  });
});
