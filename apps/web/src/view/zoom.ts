// UI zoom (#1711) — browser-style Ctrl/Cmd +/-/0 scaling of the whole console, persisted.
//
// The Tauri desktop WebView has no built-in browser zoom, so desktop users couldn't scale the
// UI at all. This applies a page zoom via the CSS `zoom` property on the document element
// (equivalent to browser zoom — it reflows layout and adjusts scrollbars, unlike a transform),
// driven by the ADR 0063 keybindings and persisted in localStorage. It behaves the same in a
// plain browser, shadowing the native Ctrl/⌘ +/-/0 for those combos like the other keybindings
// that override browser shortcuts (⌘T/⌘O/…) — all rebindable in Settings ▸ Keyboard.

const STORAGE_KEY = "pl:ui-zoom";
export const ZOOM_MIN = 0.5;
export const ZOOM_MAX = 2.0;
export const ZOOM_STEP = 0.1;
export const ZOOM_DEFAULT = 1.0;

/** Clamp to [MIN, MAX] and round to one decimal, so repeated ±0.1 steps don't drift into
 *  float noise (0.30000000000000004). A non-finite input falls back to the default. */
export function clampZoom(level: number): number {
  const n = Number.isFinite(level) ? level : ZOOM_DEFAULT;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(n * 10) / 10));
}

/** The persisted zoom (default when unset or storage is unavailable — never throws). */
export function readStoredZoom(): number {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw == null ? ZOOM_DEFAULT : clampZoom(parseFloat(raw));
  } catch {
    return ZOOM_DEFAULT; // private-mode / storage-disabled
  }
}

function writeStoredZoom(level: number): void {
  try {
    if (level === ZOOM_DEFAULT) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, String(level));
  } catch {
    /* storage disabled — zoom still applies for the session, it just won't persist */
  }
}

/** Apply a zoom level to the document (page zoom, like the browser's). Default (1.0) clears
 *  the property so there's no leftover `zoom: 1` on the html element. */
export function applyZoom(level: number): void {
  if (typeof document === "undefined") return;
  const style = document.documentElement.style;
  if (level === ZOOM_DEFAULT) style.removeProperty("zoom");
  else style.setProperty("zoom", String(level));
}

function setZoom(level: number): number {
  const next = clampZoom(level);
  writeStoredZoom(next);
  applyZoom(next);
  return next;
}

/** Step zoom up one increment; returns the new level. */
export function zoomIn(): number {
  return setZoom(readStoredZoom() + ZOOM_STEP);
}

/** Step zoom down one increment; returns the new level. */
export function zoomOut(): number {
  return setZoom(readStoredZoom() - ZOOM_STEP);
}

/** Back to 100%; returns the new level. */
export function zoomReset(): number {
  return setZoom(ZOOM_DEFAULT);
}

/** Apply the persisted zoom on boot — call before first paint to avoid a flash of unscaled UI. */
export function initZoom(): void {
  applyZoom(readStoredZoom());
}
