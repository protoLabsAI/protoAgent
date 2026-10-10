// The bearer + theme postMessage handshake every plugin-iframe host performs (ADR 0026
// theming bridge; ADR 0118). Extracted from PluginView so the later iframe hosts (the
// inline artifact host and the frame-component host) reuse the EXACT same protocol and
// origin checks rather than re-deriving them:
//   • the console theme snapshot (consoleTheme / PL_TOKEN_VARS),
//   • the targeted origin a host → frame post must use (frameOrigin),
//   • the init post that hands the page the operator bearer + theme (postInit) and its
//     load-time re-post schedule (scheduleInitReposts),
//   • the live re-theme post + its window-event hook (postTheme / usePluginFrameThemeSync).
// A host → frame post is ALWAYS targeted at the plugin page's own origin (never "*") and
// never carries the bearer in the URL.
import designTokens from "@protolabsai/design/tokens.json";
import { useEffect, type RefObject } from "react";

import { apiUrl, authToken } from "../lib/api";

// The `--pl-*` custom-property names the design package publishes, derived from its
// tokens.json exactly the way the DS build generates tokens.css: kebab-case each key
// path under a `--pl` prefix. The top-level `light` block is the light-MODE override
// set (same names, different values), not extra tokens — skip it. Exported so tests
// can pin the derived list against the shipped token set.
const kebab = (s: string) => s.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase();
function collectTokenVars(node: Record<string, unknown>, prefix: string, acc: string[]): string[] {
  for (const [key, value] of Object.entries(node)) {
    if (prefix === "--pl" && key === "light") continue;
    const name = `${prefix}-${kebab(key)}`;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      collectTokenVars(value as Record<string, unknown>, name, acc);
    } else {
      acc.push(name);
    }
  }
  return acc;
}
export const PL_TOKEN_VARS: readonly string[] = collectTokenVars(
  designTokens as Record<string, unknown>, "--pl", [],
);

// The active light/dark mode: the explicit `data-theme` force on <html> when the theme
// machinery set one (agentTheme.ts / the DS ThemePanel), else the OS preference.
function themeMode(): string {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced) return forced;
  try {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    return "dark";
  }
}

// Console theme forwarded to a plugin view so it can match the console look (ADR 0026
// theming bridge). One flat record, three layers:
//   • the original curated six keys (bg/bgPanel/fg/fgMuted/brand/border) — unchanged;
//     older plugin-kits bridge ONLY these onto --pl-* tokens, so they're the
//     backward-compat contract (#2225);
//   • the FULL computed --pl-* snapshot, keyed off @protolabsai/design's tokens.json —
//     the kit passes --pl-*-form keys straight onto the page's :root, so a view inherits
//     the operator's whole active theme (spacing, radii, status colors, fonts), not just
//     the six curated slots;
//   • `mode` — the current data-theme ("light"/"dark"), so a page can pick
//     mode-appropriate assets/color-scheme (an unknown key to older kits — ignored).
// Exported so the command palette (ADR 0057) can hand the same theme to an
// inline-morphed plugin iframe.
export function consoleTheme(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const s = getComputedStyle(document.documentElement);
  const g = (n: string) => s.getPropertyValue(n).trim();
  const theme: Record<string, string> = {
    bg: g("--pl-color-bg"), bgPanel: g("--pl-color-bg-raised"), fg: g("--pl-color-fg"),
    fgMuted: g("--pl-color-fg-muted"), brand: g("--pl-color-accent"), border: g("--pl-color-border"),
    mode: themeMode(),
  };
  for (const name of PL_TOKEN_VARS) {
    const v = g(name);
    if (v) theme[name] = v; // an unresolvable var is omitted — the kit skips empties anyway
  }
  return theme;
}

// The origin a host → frame post must target: the plugin page's OWN origin, derived from
// its src exactly as the probe and event relay do. Under the desktop app the console runs
// on `tauri://localhost` while the sidecar page is `http://127.0.0.1:7870`, so posting to
// the wrong origin is refused outright — posts must name this one, never "*". Throws if the
// src can't be parsed into a URL; callers that post swallow that (best effort).
export function frameOrigin(src: string): string {
  return new URL(apiUrl(src), window.location.href).origin;
}

// Post the bearer + theme to the iframe (`protoagent:init`). Idempotent on the kit side
// (applyTheme just re-sets CSS vars), so it's safe to call repeatedly — which the handshake
// relies on. Same origin, targeted, never a token in the URL.
export function postInit(win: Window, src: string): void {
  try {
    win.postMessage(
      { type: "protoagent:init", token: authToken() || null, theme: consoleTheme() },
      frameOrigin(src),
    );
  } catch {
    /* cross-origin / detached — best effort */
  }
}

// Re-post the FRESH theme payload to a mounted frame (`protoagent:theme`) — read at call
// time, not captured — so a view repaints on a live theme/accent switch without a reload.
export function postTheme(win: Window, src: string): void {
  try {
    win.postMessage({ type: "protoagent:theme", theme: consoleTheme() }, frameOrigin(src));
  } catch {
    /* cross-origin / detached — best effort */
  }
}

// The plugin page registers its `message` listener asynchronously (dynamic import of the
// plugin-kit), so the load-time init post can land BEFORE the kit is listening and be
// dropped — the view then renders with the kit's default theme until a manual switch (the
// "toggle around for it to load" bug). So re-post on a short schedule; a retry lands once
// the kit is ready, and postInit is idempotent so the extra posts are harmless. A newer kit
// that pings `protoagent:ready` makes the init exact; this schedule is the fallback for kits
// that only listen.
export const INIT_REPOST_DELAYS: readonly number[] = [100, 300, 700, 1500];

// Post the init once immediately, then on the re-post schedule. Returns the pending timer
// ids so the caller can clear them on unmount / frame re-point.
export function scheduleInitReposts(win: Window, src: string): number[] {
  postInit(win, src);
  return INIT_REPOST_DELAYS.map((ms) => window.setTimeout(() => postInit(win, src), ms));
}

// Live re-theme (ADR 0026/0042). The console fires a `protoagent:theme` window event on any
// theme/accent change (watchThemeChanges in agentTheme.ts observes the root's
// style/data-theme). Re-post the FRESH theme payload to the mounted iframe so the page
// repaints WITHOUT a reload. Gated on `navigatedRef`: an un-navigated frame is still
// about:blank on the console's origin (see PluginView's navigatedRef note), so a post
// targeted at the sidecar origin would be refused and nobody is listening anyway — the
// load-time init post covers the first paint. (consoleTheme() reads the now-updated :root
// vars at fire time.)
export function usePluginFrameThemeSync(
  frameRef: RefObject<HTMLIFrameElement | null>,
  src: string,
  navigatedRef: RefObject<boolean>,
): void {
  useEffect(() => {
    const onThemeChange = () => {
      const win = frameRef.current?.contentWindow;
      if (!win || !navigatedRef.current) return;
      postTheme(win, src);
    };
    window.addEventListener("protoagent:theme", onThemeChange);
    return () => window.removeEventListener("protoagent:theme", onThemeChange);
  }, [frameRef, src, navigatedRef]);
}
