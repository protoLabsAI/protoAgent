import { parseRunDeepLink, stripRunDeepLink } from "../lib/viewDeepLink";
import { openRunInspector } from "./store";

/** Apply a boot-URL `?run=<id>` (#4726): open the run inspector on it, then strip the key from
 *  the address bar so a reload or Back doesn't reopen it. Returns the id it opened, or null. */
export function applyRunDeepLink(loc: Location = window.location, history: History = window.history): string | null {
  const id = parseRunDeepLink(loc.search);
  if (!new URLSearchParams(loc.search).has("run")) return null;
  try {
    history.replaceState(history.state, "", loc.pathname + stripRunDeepLink(loc.search) + loc.hash);
  } catch {
    /* no history API (embedded webview) — the link just stays in the bar */
  }
  if (id) openRunInspector(id);
  return id;
}
