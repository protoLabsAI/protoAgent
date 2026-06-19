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
