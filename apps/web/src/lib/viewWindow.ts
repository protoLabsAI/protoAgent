// Pop a plugin view out into its own window (spike). The window boots the same bundle as
// the console, but `main.tsx` sees the `#/view/<key>` route and renders ONLY that surface
// (ViewWindow.tsx) — the launcher's pattern (ADR 0057): one bundle, a different root.
//
// Desktop: the shell builds a real managed window (`open_view_window`, navigation.rs), so
// it gets the API-base handoff, the drag-drop fix and the new-window triage every
// shell-built window shares. Browser: a plain `window.open` of the same route — the slug
// rides the pathname, so a member's view pops out against that member.

import { hasDesktopShell, invoke } from "./desktop";

const VIEW_ROUTE = /^#\/view\/(plugin:[A-Za-z0-9_.:-]+)\/?$/;

/** The plugin surface key this window was opened for, or null for a normal console. */
export function viewWindowKey(hash: string = typeof window === "undefined" ? "" : window.location.hash): string | null {
  return hash.match(VIEW_ROUTE)?.[1] ?? null;
}

/** Open `viewKey` (`plugin:<id>:<viewId>`) in its own window. */
export async function openViewWindow(viewKey: string, label?: string): Promise<void> {
  if (hasDesktopShell()) {
    await invoke("open_view_window", { viewKey, title: label ?? null });
    return;
  }
  const { pathname, search } = window.location;
  window.open(`${pathname}${search}#/view/${viewKey}`, "_blank", "popup,width=960,height=720");
}
