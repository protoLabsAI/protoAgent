import { applyStoredTheme } from "@protolabsai/ui/theming";

// Bridge the per-agent server theme (/api/theme, ADR 0042) to the DS ThemePanel, which is
// localStorage-backed (key "pl-theme", an opaque {mode, overrides} blob). The host round-trips
// that blob: GET seeds localStorage + repaints; the panel's edits persist to localStorage; a
// "Save" PUTs the current blob back. The blob is opaque — the panel owns its schema.
const PL_THEME_KEY = "pl-theme"; // @protolabsai/ui theming.tsx LS_THEME

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
 *  the initial boot apply (nothing to crossfade from, and the snapshot can disrupt first paint). */
export function applyAgentTheme(theme: unknown, animate = true) {
  const apply = () => {
    clearOverrides();
    if (theme && typeof theme === "object") {
      try {
        localStorage.setItem(PL_THEME_KEY, JSON.stringify(theme));
      } catch {
        /* ignore */
      }
      applyStoredTheme(); // re-reads the seeded blob → mode + overrides
    } else {
      try {
        localStorage.removeItem(PL_THEME_KEY);
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

// Validate + normalize a CSS color before it's interpolated into SVG/markup. The accent comes
// from the opaque, agent-supplied theme blob (--pl-color-accent), so a malformed or hostile
// token must never reach the favicon data-URI or the meta tag. Returns null if it isn't a real
// color, so callers bail instead of emitting broken markup.
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

/** Point the tab favicon + `<meta name="theme-color">` at the active theme's accent
 *  (`--pl-color-accent`), so an agent/theme switch (ADR 0042) reaches the browser chrome too —
 *  otherwise the tab stays the frozen brand default while the rest of the console repaints.
 *  With no theme active it leaves (or restores) index.html's static favicon. Fail-safe: bails
 *  if the accent isn't a valid color. */
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

  const accent = safeColor(getComputedStyle(root()).getPropertyValue("--pl-color-accent"));
  if (!accent) return;

  let link = icon;
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.type = "image/svg+xml";
  link.setAttribute("href", `data:image/svg+xml,${encodeURIComponent(faviconSvg(accent))}`);

  let mc = meta;
  if (!mc) {
    mc = document.createElement("meta");
    mc.name = "theme-color";
    document.head.appendChild(mc);
  }
  mc.setAttribute("content", accent);
  _chromeThemed = true;
}

// Broadcast a single `protoagent:theme` window event whenever the document's theme changes —
// applyAgentTheme (switch/save/reset) AND the ThemePanel's live picker edits both mutate the
// root's `style`/`data-theme`, so one MutationObserver catches everything. PluginView listens
// and re-posts the theme to its iframe, so embedded plugin views repaint live too (ADR 0026/0042);
// the same hook keeps the tab favicon + theme-color on the active accent.
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
