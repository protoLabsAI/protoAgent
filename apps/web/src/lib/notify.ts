// Native notification when the window isn't in front — so a HITL form (or other
// alert) reaches the operator even when the desktop app is hidden (menu-bar-only)
// or the browser tab is backgrounded. Uses the standard Web Notification API,
// which the Tauri desktop bridges via tauri-plugin-notification (capability
// `notification:default`); in a browser it's the native API. No-op when the
// window is already focused (don't nag when they're looking) or unsupported.

let permissionAsked = false;

export function notifyIfHidden(title: string, body?: string): void {
  if (typeof window === "undefined" || typeof Notification === "undefined") return;
  // Only alert when the user isn't already looking at the window.
  const visible = document.visibilityState === "visible" && document.hasFocus();
  if (visible) return;

  const fire = () => {
    try {
      new Notification(title, { body: body?.slice(0, 240) });
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
