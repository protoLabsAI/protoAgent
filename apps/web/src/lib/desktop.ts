// Thin, dependency-free accessors for the Tauri desktop shell's global API
// (`withGlobalTauri: true` in tauri.conf.json exposes `window.__TAURI__`), so the
// shared web bundle needs no `@tauri-apps/api` dependency. Everything degrades to a
// no-op in the browser, so callers can use these unconditionally.

type TauriCore = {
  invoke: <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
};
type UnlistenFn = () => void;
type TauriEvent = {
  emit: (event: string, payload?: unknown) => Promise<void>;
  listen: <T = unknown>(event: string, handler: (e: { payload: T }) => void) => Promise<UnlistenFn>;
};
type TauriGlobal = { core?: TauriCore; event?: TauriEvent };

function tauri(): TauriGlobal | null {
  try {
    return (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__ ?? null;
  } catch {
    return null;
  }
}

/** True when THIS webview is the frameless quick-launcher window (the Rust shell injects
 *  `window.__PROTOAGENT_LAUNCHER__` only on that window). */
export function isLauncherWindow(): boolean {
  try {
    return Boolean((window as unknown as { __PROTOAGENT_LAUNCHER__?: boolean }).__PROTOAGENT_LAUNCHER__);
  } catch {
    return false;
  }
}

/** Invoke a Tauri command; resolves to undefined (no-op) outside the desktop shell. */
export async function invoke<T = unknown>(cmd: string, args?: Record<string, unknown>): Promise<T | undefined> {
  const core = tauri()?.core;
  if (!core) return undefined;
  try {
    return await core.invoke<T>(cmd, args);
  } catch {
    return undefined;
  }
}

/** Emit a Tauri event to every window; no-op outside the desktop shell. */
export async function emit(event: string, payload?: unknown): Promise<void> {
  await tauri()?.event?.emit(event, payload).catch(() => {});
}

/** Listen for a Tauri event. Returns an unlisten fn (a no-op outside the shell), so
 *  callers can `void listen(...).then(off => ...)` and clean up uniformly. */
export async function listen<T = unknown>(
  event: string,
  handler: (payload: T) => void,
): Promise<UnlistenFn> {
  const ev = tauri()?.event;
  if (!ev) return () => {};
  try {
    return await ev.listen<T>(event, (e) => handler(e.payload));
  } catch {
    return () => {};
  }
}

/**
 * Ask the desktop shell for the operator token its own sidecar is configured with (#2055).
 *
 * The app spawns that server and sets its `PROTOAGENT_HOME`, so it already has the secret —
 * making the operator hunt through `secrets.yaml` to unlock an app on their own machine was
 * never defensible. Returns null in a browser, or when no token is configured (the normal
 * loopback case), and callers fall through to the token prompt.
 *
 * Over `invoke` deliberately: `initialization_script` is documented as unreliable across
 * Tauri v2 webview contexts (hence the `?__apiPort=` handoff), and a token must never ride
 * the webview URL, which is readable by the page and anything it embeds.
 */
export async function desktopAuthToken(): Promise<string | null> {
  const core = tauri()?.core;
  if (!core) return null;
  try {
    const token = await core.invoke<string | null>("auth_token");
    return typeof token === "string" && token.trim() ? token.trim() : null;
  } catch {
    // An older shell without the command, or a denied capability — degrade to the prompt
    // rather than blocking boot on an optional convenience.
    return null;
  }
}
