// The console theme, inlined into the streamed artifact preview (ADR 0118 D3, #4076).
//
// The live inline artifact frame is themed by the artifact plugin's shell (plugins/artifact/
// shell.js `base()`): the console's full `--pl-*` palette as literal values, a `color-scheme`
// that follows the CONSOLE (not the OS), html/body painted from the tokens, zero-specificity
// `:where()` control defaults, and the DS plugin-kit stylesheet. The streamed PREVIEW that
// stands in for that frame while the artifact is still being written must look the same — else
// it reads as a white card (OS light, console dark) or bare browser controls, then jumps to the
// themed frame at handover. Its CSP is `default-src 'none'`, so a `<link>` to the kit can't
// load: the kit's CSS TEXT is fetched once per page, cached, and inlined instead (the CSP stays
// tight — no network origin is ever allowed into the preview).

import { consoleTheme, PL_TOKEN_VARS } from "../app/pluginFrameHandshake";
import { apiUrl } from "../lib/api";

/** The token names the shell carries into every live artifact frame — mirrors `THEME_TOKENS` in
 *  plugins/artifact/shell.js — unioned with the design package's own token list, so the preview
 *  resolves every `var(--pl-…)` the live frame does (chart series included). */
const SHELL_THEME_TOKENS: readonly string[] = [
  "--pl-color-bg", "--pl-color-fg", "--pl-color-fg-muted", "--pl-color-accent", "--pl-color-border",
  "--pl-font-sans", "--pl-color-bg-raised", "--pl-color-bg-subtle", "--pl-color-bg-hover",
  "--pl-color-bg-inset", "--pl-color-fg-subtle", "--pl-color-fg-on-accent", "--pl-color-accent-hover",
  "--pl-color-accent-fg", "--pl-color-border-strong", "--pl-color-status-error", "--pl-color-status-info",
  "--pl-color-status-success", "--pl-color-status-warning", "--pl-font-mono", "--pl-radius", "--pl-radius-lg",
  "--pl-color-chart-axis", "--pl-color-chart-grid",
  ...Array.from({ length: 8 }, (_, i) => `--pl-color-chart-series${i + 1}`),
];

/** The shell's themed defaults for UNSTYLED elements (shell.js `DEFAULTS`), minus its literal
 *  fallbacks — the preview always carries the full token set (and the console bans `var(--pl-…,
 *  <literal>)` fallbacks, tokenNameGuard). Zero
 *  specificity, so any rule the artifact writes wins outright. */
export const PREVIEW_CONTROL_DEFAULTS =
  ":where(html,body){font-family:var(--pl-font-sans);line-height:1.5}" +
  ":where(input,select,textarea){font:inherit;color:var(--pl-color-fg);background:var(--pl-color-bg-inset);" +
  "border:1px solid var(--pl-color-border);border-radius:var(--pl-radius);padding:6px 8px}" +
  ":where(input,select,textarea):focus{outline:2px solid var(--pl-color-accent);outline-offset:1px}" +
  ":where(button){font:inherit;color:var(--pl-color-fg);background:var(--pl-color-bg-raised);" +
  "border:1px solid var(--pl-color-border);border-radius:var(--pl-radius);padding:6px 12px;cursor:pointer}" +
  ":where(button):hover{background:var(--pl-color-bg-hover,var(--pl-color-bg-raised))}" +
  ":where(a){color:var(--pl-color-accent)}" +
  ":where(table){border-collapse:collapse}:where(th,td){border-bottom:1px solid var(--pl-color-border);padding:6px 8px;text-align:left}";

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

/** A token value safe to inline into a `<style>`: no declaration/rule/tag breakout. */
function safeValue(v: string): boolean {
  return !/[;{}<>]/.test(v);
}

/** The console's live theme as one stylesheet for the preview frame: every `--pl-*` token as a
 *  literal value plus `color-scheme`, under `:root[data-theme]` (the preview's `<html>` carries the
 *  same `data-theme`) so it outranks the kit's own `:root` defaults AND its OS-driven
 *  `prefers-color-scheme` block; html/body painted from the tokens; then the control defaults. */
export function previewThemeCss(): string {
  if (typeof window === "undefined") return "";
  const cs = getComputedStyle(document.documentElement);
  const names = new Set<string>([...PL_TOKEN_VARS, ...SHELL_THEME_TOKENS]);
  let decl = "";
  for (const name of names) {
    const v = cs.getPropertyValue(name).trim();
    if (v && safeValue(v)) decl += `${name}:${v};`;
  }
  const scheme = consoleScheme();
  return (
    `:root[data-theme]{${decl}color-scheme:${scheme}}` +
    "html,body{margin:0;background:var(--pl-color-bg);color:var(--pl-color-fg)}" +
    PREVIEW_CONTROL_DEFAULTS
  );
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

/** Test seam: forget the cached kit. */
export function resetPluginKitCssForTest(): void {
  kitCss = null;
  kitPromise = null;
}
