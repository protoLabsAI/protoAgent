// Native notification when the window isn't in front — so a HITL form (or other
// alert) reaches the operator even when the desktop app is hidden (menu-bar-only)
// or the browser tab is backgrounded. Uses the standard Web Notification API,
// which the Tauri desktop bridges via tauri-plugin-notification (capability
// `notification:default`); in a browser it's the native API. No-op when the
// window is already focused (don't nag when they're looking) or unsupported.

import { apiUrl } from "./api/routing";
import { hasDesktopShell } from "./desktop";

let permissionAsked = false;

export type NotifyOptions = {
  /** This alert mirrors a hub NOTICE (ADR 0135 W2): the server also raised it through
   *  `notify_chat_surfaces`, so it is on the event stream as a `notice` event. Set it only
   *  for those — today a new decision and a scheduled result card. */
  notice?: boolean;
  /** The hub notice identity, shared with the push worker notification tag. */
  noticeId?: string;
};

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

/** True when the desktop shell's native receiver owns notice presentation for this window
 *  (ADR 0135 W2, "one presentation owner per surface"). The shell reads its OWN hub's
 *  event stream (`/api/events` on the local sidecar) and shows every `notice` as an OS
 *  notification, window open or closed to the tray — so a console window on that same
 *  stream must not raise a second banner for the same thing. Standing down loses nothing:
 *  the shell asks the hub to replay from its saved cursor (from the start on a first run),
 *  so a notice raised before it connected, or still pending at launch, is shown as well. A window focused on a fleet
 *  member (its events come through `/agents/<slug>/…`) or pointed at another machine is
 *  reading a stream the shell does not watch, so it keeps its own notifications. */
export function nativeOwnsNotices(): boolean {
  if (!hasDesktopShell()) return false;
  try {
    const events = new URL(apiUrl("/api/events"), window.location.href);
    return LOOPBACK_HOSTS.has(events.hostname) && events.pathname === "/api/events";
  } catch {
    return false;
  }
}

// `onClick` (optional): when the operator clicks the notification, bring the window to the
// front and run it — the "Decisions" notification uses it to open the panel (#4725, §4.5).
// `opts.notice` (optional): the alert duplicates a hub notice, so it is left to the desktop
// shell where that owns presentation. In a plain browser the flag changes nothing.
// Existing two- and three-arg callers are unchanged.
export function notifyIfHidden(
  title: string,
  body?: string,
  onClick?: () => void,
  opts?: NotifyOptions,
): void {
  if (opts?.notice && nativeOwnsNotices()) return;
  if (typeof window === "undefined" || typeof Notification === "undefined") return;
  // Only alert when the user isn't already looking at the window.
  const visible = document.visibilityState === "visible" && document.hasFocus();
  if (visible) return;

  const fire = () => {
    try {
      const notification = new Notification(title, { body: body?.slice(0, 240), ...(opts?.noticeId ? { tag: opts.noticeId } : {}) });
      if (onClick) {
        notification.onclick = () => {
          try {
            window.focus();
          } catch {
            // focus() can throw in some webviews — the onClick still runs.
          }
          onClick();
        };
      }
    } catch {
      // Some webview/permission states throw on construction — ignore.
    }
  };

  if (Notification.permission === "granted") {
    fire();
  } else if (Notification.permission !== "denied" && !permissionAsked) {
    permissionAsked = true;
    void Notification.requestPermission().then((p) => {
      if (p === "granted") fire();
    });
  }
}
