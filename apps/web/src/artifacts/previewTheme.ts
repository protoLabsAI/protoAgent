// The console theme, inlined into the streamed artifact preview (ADR 0118 D3, #4076).
//
// The live inline artifact frame is themed by the artifact plugin's shell (plugins/artifact/
// shell.js `base()`): the console's full `--pl-*` palette as literal values, a `color-scheme`
// that follows the CONSOLE (not the OS), html/body painted from the tokens, zero-specificity
// `:where()` control defaults, and the DS plugin-kit stylesheet. The streamed PREVIEW that
// stands in for that frame while the artifact is still being written must look the same — else
// it reads as a white card (OS light, console dark) or bare browser controls, then jumps to the
// themed frame at handover. Its CSP is `default-src 'none'`, so a `<link>` to the kit or the
// control-default sheet can't load: their CSS TEXT is fetched once per page, cached, and inlined
// instead (the CSP stays tight — no network origin is ever allowed into the preview).
//
// The DS element defaults (the `:where()` control resets, ported from the artifact shell's
// `DEFAULTS`) are NOT bundled into apps/web. They are vendored ONCE into the artifact plugin — the
// DS `styles/embed.css` sheet, served from its same-origin vendor route — the single vendored copy
// the live artifact frame also loads. The preview FETCHES that route at runtime, exactly as it
// fetches the kit (protoContent#577): a runtime fetch, never a build-time console dependency.

import { EMBED_TOKENS, snapshotToCss, themeTokenSnapshot } from "@protolabsai/design/embed";

import { consoleTheme, PL_TOKEN_VARS } from "../app/pluginFrameHandshake";
import { apiUrl } from "../lib/api";

/** The token names carried into the preview: the DS embed set (`EMBED_TOKENS` — the colour palette
 *  with the chart series, type, radius and spacing scales; a superset of the `THEME_TOKENS` the
 *  artifact shell carries into every live frame) unioned with the design package's full token list,
 *  so the preview resolves every `var(--pl-…)` the live frame does. */
const PREVIEW_TOKENS: readonly string[] = [...new Set([...EMBED_TOKENS, ...PL_TOKEN_VARS])];

/** The console's light/dark scheme — what its OWN palette renders as (the root's computed
 *  `color-scheme`, which the theme machinery sets from the operator's theme), falling back to the
 *  handshake's mode. Never the OS preference alone: an operator on a dark console with a light OS
 *  must get a dark preview. */
export function consoleScheme(): "light" | "dark" {
  if (typeof window === "undefined") return "dark";
  const cs = (getComputedStyle(document.documentElement).colorScheme || "").trim();
  if (cs.startsWith("light")) return "light";
  if (cs.startsWith("dark")) return "dark";
  return consoleTheme().mode === "light" ? "light" : "dark";
}

/** The console's live theme as one stylesheet for the preview frame: every `--pl-*` token as a
 *  literal value plus `color-scheme` (the DS embed snapshot, whose serializer drops any value that
 *  could break out of the rule), under `:root[data-theme]` (the preview's `<html>` carries the same
 *  `data-theme`) so it outranks the kit's own `:root` defaults AND its OS-driven
 *  `prefers-color-scheme` block; html/body painted from the tokens; then the control defaults. */
export function previewThemeCss(): string {
  if (typeof window === "undefined") return "";
  const snapshot = themeTokenSnapshot(document.documentElement, PREVIEW_TOKENS);
  // The CONSOLE's scheme, normalised to one keyword — never the OS's, and never "light dark".
  snapshot["color-scheme"] = consoleScheme();
  return (
    snapshotToCss(snapshot, ":root[data-theme]") +
    "html,body{margin:0;background:var(--pl-color-bg);color:var(--pl-color-fg)}" +
    cachedControlDefaults()
  );
}

// ── The DS element-default control sheet, fetched once and cached ───────────────────────────

let controlDefaultsCss: string | null = null;
let controlDefaultsPromise: Promise<string> | null = null;

/** The DS element defaults (the `:where()` control resets) if already loaded, else "" — the
 *  preview inlines whatever is cached when {@link previewThemeCss} is read, and the rest arrives
 *  once {@link loadControlDefaults} settles and the theme is re-posted (the same shape as the kit
 *  below). Zero-specificity, so any rule the artifact writes still wins outright. */
export function cachedControlDefaults(): string {
  return controlDefaultsCss ?? "";
}

/** Fetch the DS element-default sheet ONCE per page and cache its (inline-safe) text. The sheet is
 *  `@protolabsai/ui-css` `styles/embed.css`, vendored into the artifact plugin and served from its
 *  same-origin vendor route — the single vendored copy the live artifact frame also loads, never a
 *  build-time import under apps/web and never a `<link>` (the preview CSP has no network origin).
 *  Resolves "" on any failure — the preview is then themed by the tokens alone, never left blank. A
 *  failed fetch is retried on the next call. */
export function loadControlDefaults(): Promise<string> {
  if (controlDefaultsCss !== null) return Promise.resolve(controlDefaultsCss);
  if (controlDefaultsPromise) return controlDefaultsPromise;
  controlDefaultsPromise = (async () => {
    try {
      const res = await fetch(apiUrl("/plugins/artifact/vendor/pl-embed.css", { host: true }));
      if (!res.ok) throw new Error(`pl-embed.css ${res.status}`);
      controlDefaultsCss = inlineSafeCss(await res.text());
      return controlDefaultsCss;
    } catch {
      controlDefaultsPromise = null; // retry next time
      return "";
    }
  })();
  return controlDefaultsPromise;
}

// ── The DS plugin-kit stylesheet, fetched once and cached ───────────────────────────────────

let kitCss: string | null = null;
let kitPromise: Promise<string> | null = null;

/** Make CSS text safe to inline in a srcdoc `<style>`: a literal `<` (only ever in a comment or
 *  string in real CSS) becomes the CSS escape `\3c `, so nothing can close the style element. */
export function inlineSafeCss(css: string): string {
  return css.replace(/</g, "\\3c ");
}

/** The kit CSS if it has already loaded, else "" — the preview's srcdoc inlines whatever is
 *  cached at mount and the rest arrives over postMessage once {@link loadPluginKitCss} settles. */
export function cachedPluginKitCss(): string {
  return kitCss ?? "";
}

/** Fetch `/_ds/plugin-kit.css` ONCE per page and cache its (inline-safe) text. Resolves "" on any
 *  failure — the preview is then themed by the tokens + defaults alone, never left blank. A
 *  failed fetch is retried on the next call. */
export function loadPluginKitCss(): Promise<string> {
  if (kitCss !== null) return Promise.resolve(kitCss);
  if (kitPromise) return kitPromise;
  kitPromise = (async () => {
    try {
      const res = await fetch(apiUrl("/_ds/plugin-kit.css", { host: true }));
      if (!res.ok) throw new Error(`plugin-kit.css ${res.status}`);
      kitCss = inlineSafeCss(await res.text());
      return kitCss;
    } catch {
      kitPromise = null; // retry next time
      return "";
    }
  })();
  return kitPromise;
}

/** Test seam: forget the cached kit AND the cached DS control defaults. */
export function resetPluginKitCssForTest(): void {
  kitCss = null;
  kitPromise = null;
  controlDefaultsCss = null;
  controlDefaultsPromise = null;
}
