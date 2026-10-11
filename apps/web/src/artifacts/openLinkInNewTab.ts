// The host side of an approved openLink() (ADR 0118 D4, #4076 F1): actually open the tab, and
// say truthfully whether it opened so the frame's Promise never reports a blocked pop-up as done.

import { hasDesktopShell } from "../lib/desktop";
import { OPEN_LINK_FEATURES } from "./frameBridge";

/**
 * Open `url` in a new tab for the operator and report HONESTLY whether it opened (#4076 F1).
 *
 * `window.open(url, "_blank", "noopener,noreferrer")` returns null on success AND when the pop-up
 * blocker refused it (the spec returns null whenever noopener/noreferrer is set), so it can't tell
 * the frame the truth. In a browser this opens a blank tab WITHOUT those features (null now means
 * blocked), severs `opener` at once, then navigates the tab from INSIDE itself through an anchor
 * with `referrerpolicy="no-referrer"` — the destination gets no opener and no Referer, exactly as
 * {@link OPEN_LINK_FEATURES} promises.
 *
 * In the desktop shell the webview never creates the tab: its new-window handler hands the URL to
 * the system browser and denies the request, so `window.open` returns null by design there. That
 * path keeps the plain feature-string call and reports success — the shell owns the open.
 */
export function openLinkInNewTab(
  url: string,
  env: { open?: Window["open"]; desktopShell?: boolean } = {},
): boolean {
  const open = env.open ?? ((...a: Parameters<Window["open"]>) => window.open(...a));
  const desktopShell = env.desktopShell ?? hasDesktopShell();
  if (desktopShell) {
    open(url, "_blank", OPEN_LINK_FEATURES);
    return true;
  }
  let w: Window | null = null;
  try {
    w = open("about:blank", "_blank");
  } catch {
    return false;
  }
  if (!w) return false; // blocked
  try {
    w.opener = null;
  } catch {
    /* best effort — the navigation below is what matters */
  }
  try {
    const doc = w.document;
    const a = doc.createElement("a");
    a.href = url;
    a.rel = "noopener noreferrer";
    a.referrerPolicy = "no-referrer";
    (doc.body ?? doc.documentElement).appendChild(a);
    a.click();
  } catch {
    // Can't script the blank tab (shouldn't happen — it is same-origin about:blank). Never leave
    // a stray blank tab claiming success: close it and report the link as not opened.
    try {
      w.close();
    } catch {
      /* ignore */
    }
    return false;
  }
  return true;
}
