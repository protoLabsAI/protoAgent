/**
 * Desktop (Tauri) shell detection + the typed `window.__TAURI__.core` accessor, split out of
 * `lib/api.ts` (#3822) so the chat and runtime slices share it. Leaf module.
 */

/** True inside the desktop (Tauri/WKWebView) shell. WKWebView does NOT deliver a
 * `text/event-stream` body through `fetch()` — neither via `body.getReader()` nor
 * a buffered `clone().text()` (both come back empty) — so the streaming chat turn
 * renders as a blank assistant bubble. In that environment we route the chat turn
 * through the non-streaming `/api/chat` endpoint instead, which returns ordinary
 * JSON that WKWebView handles fine (it's how the rest of the console already talks
 * to the sidecar). Browsers keep the streaming `/a2a` path. */
export function isDesktopWebview(): boolean {
  try {
    const { protocol, hostname } = window.location;
    return protocol === "tauri:" || protocol === "file:" || hostname === "tauri.localhost";
  } catch {
    return false;
  }
}

/** A typed view of the bits of the Tauri `core` API the desktop streaming path uses,
 * read off the `window.__TAURI__` global (the shell sets `withGlobalTauri: true`), so
 * the shared web bundle needs no `@tauri-apps/api` dependency. Null outside the shell. */
type TauriChannel<T> = { onmessage: (msg: T) => void };
type TauriCore = {
  invoke: <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
  Channel: new <T>() => TauriChannel<T>;
};
export function tauriCore(): TauriCore | null {
  try {
    return (window as unknown as { __TAURI__?: { core?: TauriCore } }).__TAURI__?.core ?? null;
  } catch {
    return null;
  }
}
