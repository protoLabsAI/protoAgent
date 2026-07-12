import { applyStoredTheme } from "@protolabsai/ui/theming";

import { currentSlug } from "./api";
import { resolveThemeToPersist } from "./themeMerge";

// Bridge the per-agent server theme (/api/theme, ADR 0042) to the DS ThemePanel, which is
// localStorage-backed (key "pl-theme", an opaque {mode, overrides} blob). The host round-trips
// that blob: GET seeds localStorage + repaints; the panel's edits persist to localStorage; a
// "Save" PUTs the current blob back. The blob is opaque — the panel owns its schema.
const PL_THEME_KEY = "pl-theme"; // @protolabsai/ui theming.tsx LS_THEME

// `pl-theme` is a SINGLE, global localStorage key shared by every same-origin agent window
// (the fleet console is slug-routed on one origin, ADR 0042). So the blob sitting in it may
// belong to a DIFFERENT agent — whichever window last wrote it. We stamp a companion key with
// the focused agent's slug on every write so boot can tell "my unsaved tweak" from "another
// agent's saved theme left behind" before deciding whether to merge (see #1762 blocker).
const PL_THEME_OWNER_KEY = "pl-theme:agent";

function root(): HTMLElement {
  return document.documentElement;
}

/** Clear any inline --pl-* overrides the panel applied, so switching to an agent with a
 *  different (or no) theme doesn't leave the previous one's colors stuck. */
function clearOverrides() {
  const s = root().style;
  for (let i = s.length - 1; i >= 0; i--) {
    const p = s[i];
    if (p.startsWith("--pl-")) s.removeProperty(p);
  }
}

/** Apply a server theme blob to the document (+ seed the panel's localStorage). `null`/empty
 *  resets to the design-system defaults. Crossfades via the View Transitions API where
 *  available (`animate`), so switching agents eases between looks — pass `animate: false` for
 *  the initial boot apply (nothing to crossfade from, and the snapshot can disrupt first paint).
 *
 *  `preservePersisted` (the boot apply, #1762): the user's persisted working copy WINS over the
 *  incoming agent default — so an unsaved tweak survives a reload and defaults only fill the
 *  gaps, instead of the default blob clobbering the user's overrides on every mount. On an
 *  explicit switch/reset (the default) we adopt the incoming theme verbatim (ADR 0042). */
export function applyAgentTheme(theme: unknown, opts: { animate?: boolean; preservePersisted?: boolean } = {}) {
  const { animate = true, preservePersisted = false } = opts;
  const apply = () => {
    clearOverrides();
    // Boot merges persisted user overrides OVER the agent default; switch/reset replaces.
    const blob = resolveThemeToPersist(theme, currentThemeBlob(), { preservePersisted });
    if (blob) {
      try {
        localStorage.setItem(PL_THEME_KEY, JSON.stringify(blob));
        localStorage.setItem(PL_THEME_OWNER_KEY, currentSlug()); // stamp the owning agent
      } catch {
        /* ignore */
      }
      applyStoredTheme(); // re-reads the seeded blob → mode + overrides
    } else {
      try {
        localStorage.removeItem(PL_THEME_KEY);
        localStorage.removeItem(PL_THEME_OWNER_KEY);
      } catch {
        /* ignore */
      }
      root().removeAttribute("data-theme"); // back to the OS/default
    }
  };

  const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
  if (animate && typeof doc.startViewTransition === "function") {
    doc.startViewTransition(apply);
  } else {
    apply();
  }
}

// The tab favicon — mirrors apps/web/public/protolabs-icon-outline.svg, with the stroke
// color injected at runtime. A static favicon SVG can't read the page's CSS vars (browsers
// render it in an isolated context where `currentColor` resolves to black), so when a theme
// is active we swap in a recolored data-URI. Keep the geometry in sync with the source asset.
const faviconSvg = (color: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-label="protoLabs">` +
  `<g transform="translate(224, 32) scale(-8, 8)" fill="none" stroke="${color}" stroke-width="2" ` +
  `stroke-linecap="round" stroke-linejoin="round">` +
  `<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/>` +
  `<path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>` +
  `</g></svg>`;

// Validate + normalize a CSS color before it's interpolated into SVG/markup. The accent and
// surface come from the opaque, agent-supplied theme blob (--pl-* overrides), so a malformed or
// hostile token must never reach the favicon data-URI or the meta tag. Returns null if it isn't
// a real color, so callers bail instead of emitting broken markup.
function safeColor(value: string): string | null {
  const v = value.trim();
  if (!v) return null;
  if (typeof CSS !== "undefined" && typeof CSS.supports === "function" && !CSS.supports("color", v)) return null;
  const probe = document.createElement("span");
  probe.style.color = v; // the browser drops anything it can't parse as a color
  return probe.style.color || null;
}

// Is a per-agent theme currently applied (vs. the shipped DS defaults)? The theme machinery
// sets `data-theme` and inline `--pl-*` overrides on <html>; absent both, we leave index.html's
// static favicon/meta in place (and the base-path regression guard that e2e/assets.spec.ts owns).
function isThemed(): boolean {
  const r = root();
  if (r.hasAttribute("data-theme")) return true;
  const s = r.style;
  for (let i = 0; i < s.length; i++) if (s[i].startsWith("--pl-")) return true;
  return false;
}

// Shipped defaults from index.html, snapshotted (raw attributes) before we first mutate them,
// so clearing a theme restores the exact static favicon + brand theme-color.
let _chromeDefaults: { iconHref: string | null; themeColor: string | null } | null = null;
let _chromeThemed = false;

/** Sync the browser chrome to the active theme, so an agent/theme switch (ADR 0042) reaches
 *  it too — otherwise the tab stays the frozen brand default while the rest of the console
 *  repaints. Two DIFFERENT tokens on purpose:
 *   - the tab favicon takes the theme's accent (`--pl-color-accent`) — the tab's brand splash;
 *   - `<meta name="theme-color">` takes the theme's surface background (`--pl-color-bg` — what
 *     the app shell/topbar actually paint via the `--bg` bridge in theme-base.css). On mobile
 *     (PWA/webview) this meta colors the status-bar/notch band ABOVE the header, and the accent
 *     there rendered a broken two-tone header (#1923); the surface color keeps the notch a
 *     continuous extension of the header. getComputedStyle resolves the mode-appropriate
 *     (light/dark) value.
 *  With no theme active it leaves (or restores) index.html's static favicon + brand theme-color.
 *  Fail-safe per token: an invalid color just leaves that piece of chrome untouched. */
export function syncBrowserChrome() {
  if (typeof document === "undefined") return;
  const icon = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!_chromeDefaults) {
    _chromeDefaults = { iconHref: icon?.getAttribute("href") ?? null, themeColor: meta?.getAttribute("content") ?? null };
  }

  if (!isThemed()) {
    // Restore the shipped static chrome only if WE themed it — otherwise don't touch the
    // default favicon link at all (keeps it a real, fetchable asset, not a data-URI).
    if (_chromeThemed) {
      if (icon && _chromeDefaults.iconHref != null) icon.setAttribute("href", _chromeDefaults.iconHref);
      if (meta && _chromeDefaults.themeColor != null) meta.setAttribute("content", _chromeDefaults.themeColor);
      _chromeThemed = false;
    }
    return;
  }

  const styles = getComputedStyle(root());
  const accent = safeColor(styles.getPropertyValue("--pl-color-accent"));
  const surface = safeColor(styles.getPropertyValue("--pl-color-bg"));

  if (accent) {
    let link = icon;
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.type = "image/svg+xml";
    link.setAttribute("href", `data:image/svg+xml,${encodeURIComponent(faviconSvg(accent))}`);
    _chromeThemed = true;
  }

  if (surface) {
    let mc = meta;
    if (!mc) {
      mc = document.createElement("meta");
      mc.name = "theme-color";
      document.head.appendChild(mc);
    }
    mc.setAttribute("content", surface);
    _chromeThemed = true;
  }
}

// Broadcast a single `protoagent:theme` window event whenever the document's theme changes —
// applyAgentTheme (switch/save/reset) AND the ThemePanel's live picker edits both mutate the
// root's `style`/`data-theme`, so one MutationObserver catches everything. PluginView listens
// and re-posts the theme to its iframe, so embedded plugin views repaint live too (ADR 0026/0042);
// the same hook keeps the tab favicon on the active accent + theme-color on the active surface.
let _watching = false;
export function watchThemeChanges() {
  if (_watching || typeof window === "undefined") return;
  _watching = true;
  let raf = 0;
  const fire = () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      syncBrowserChrome();
      window.dispatchEvent(new Event("protoagent:theme"));
    });
  };
  new MutationObserver(fire).observe(root(), { attributes: true, attributeFilter: ["style", "data-theme"] });
  syncBrowserChrome(); // initial sync — covers a theme applied before the observer was wired
}

/** The panel's current blob (what "Save to this agent" PUTs), or null if untouched. */
export function currentThemeBlob(): unknown {
  try {
    const raw = localStorage.getItem(PL_THEME_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

/** Does the persisted `pl-theme` blob belong to the currently-focused agent? Because
 *  `pl-theme` is a single GLOBAL key shared across every same-origin agent window, another
 *  agent's saved theme (written by a different window's switch/boot) can be sitting in it.
 *  Only when it belongs to THIS agent is it safe to MERGE the persisted working copy over the
 *  incoming server default on boot (#1762) — otherwise the merge bleeds the wrong agent's
 *  tokens over this agent's saved look, breaking the ADR 0042 boot contract. The DS ThemePanel
 *  writes `pl-theme` on live edits without touching the owner stamp, but those edits only ever
 *  target the focused agent, so the last applyAgentTheme stamp stays correct until a different
 *  agent's window overwrites the shared key. */
export function persistedThemeIsForCurrentAgent(): boolean {
  try {
    return localStorage.getItem(PL_THEME_OWNER_KEY) === currentSlug();
  } catch {
    return false;
  }
}
