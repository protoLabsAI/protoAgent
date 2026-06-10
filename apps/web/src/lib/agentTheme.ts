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
      root().setAttribute("data-theme", "dark"); // the console's dark baseline — NOT the OS
      // default. The @protolabsai/design tokens follow @media (prefers-color-scheme) when
      // unpinned, so removing the attribute on a light-mode OS drifts DS components light
      // while the hand-rolled chrome stays dark. Pin dark; an explicit theme still overrides.
    }
  };

  const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
  if (animate && typeof doc.startViewTransition === "function") {
    doc.startViewTransition(apply);
  } else {
    apply();
  }
}

// Broadcast a single `protoagent:theme` window event whenever the document's theme changes —
// applyAgentTheme (switch/save/reset) AND the ThemePanel's live picker edits both mutate the
// root's `style`/`data-theme`, so one MutationObserver catches everything. PluginView listens
// and re-posts the theme to its iframe, so embedded plugin views repaint live too (ADR 0026/0042).
let _watching = false;
export function watchThemeChanges() {
  if (_watching || typeof window === "undefined") return;
  _watching = true;
  let raf = 0;
  const fire = () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => window.dispatchEvent(new Event("protoagent:theme")));
  };
  new MutationObserver(fire).observe(root(), { attributes: true, attributeFilter: ["style", "data-theme"] });
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
