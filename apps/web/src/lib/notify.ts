// Native notification when the window isn't in front — so a HITL form (or other
// alert) reaches the operator even when the desktop app is hidden (menu-bar-only)
// or the browser tab is backgrounded. Uses the standard Web Notification API,
// which the Tauri desktop bridges via tauri-plugin-notification (capability
// `notification:default`); in a browser it's the native API. No-op when the
// window is already focused (don't nag when they're looking) or unsupported.

let permissionAsked = false;

// `onClick` (optional): when the operator clicks the notification, bring the window to the
// front and run it — the "Decisions" notification uses it to open the panel (#4725, §4.5).
// Existing two-arg callers are unchanged.
export function notifyIfHidden(title: string, body?: string, onClick?: () => void): void {
  if (typeof window === "undefined" || typeof Notification === "undefined") return;
  // Only alert when the user isn't already looking at the window.
  const visible = document.visibilityState === "visible" && document.hasFocus();
  if (visible) return;

  const fire = () => {
    try {
      const notification = new Notification(title, { body: body?.slice(0, 240) });
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
