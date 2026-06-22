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
// render it in an isolated context where `currentColor` resolves to black), so we rebuild a
// data-URI whenever the theme changes. Keep the geometry in sync with the source asset.
const faviconSvg = (color: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-label="protoLabs">` +
  `<g transform="translate(224, 32) scale(-8, 8)" fill="none" stroke="${color}" stroke-width="2" ` +
  `stroke-linecap="round" stroke-linejoin="round">` +
  `<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/>` +
  `<path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>` +
  `</g></svg>`;

/** Point the tab favicon + `<meta name="theme-color">` at the active theme's accent
 *  (`--pl-color-accent`), so an agent/theme switch (ADR 0042) reaches the browser chrome
 *  too — otherwise the tab stays the frozen brand default while the rest of the console
 *  repaints. Fail-safe: bails if the var doesn't resolve, leaving the static default. */
export function syncBrowserChrome() {
  if (typeof document === "undefined") return;
  const accent = getComputedStyle(root()).getPropertyValue("--pl-color-accent").trim();
  if (!accent) return;

  let icon = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
  if (!icon) {
    icon = document.createElement("link");
    icon.rel = "icon";
    document.head.appendChild(icon);
  }
  icon.type = "image/svg+xml";
  icon.href = `data:image/svg+xml,${encodeURIComponent(faviconSvg(accent))}`;

  let meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  meta.content = accent;
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
