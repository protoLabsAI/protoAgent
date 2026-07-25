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

/** True when this webview is running inside the desktop shell (so `invoke` reaches a
 *  command rather than no-opping). Callers that need to tell "the shell said no" from
 *  "there is no shell" have to ask this FIRST — `invoke` collapses both to undefined. */
export function hasDesktopShell(): boolean {
  return Boolean(tauri()?.core);
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
 * Whether the NATIVE OS chooser is a safe answer for a path setting (#2265), or the
 * in-app server-side browser (#2264) has to be used.
 *
 * A native chooser can only name a path on THIS machine, so it's correct only when the
 * config being edited belongs to an agent whose filesystem IS this machine. The desktop
 * app guarantees that for its own host window and nowhere else: a slug window focused on
 * a fleet member may be proxied to another box entirely — a registered remote, a tailnet
 * peer, a container — and picking a local folder there writes a path that doesn't exist
 * on the machine that has to resolve it. That's precisely the failure the server-side
 * browser was built to prevent, and re-introducing it for a nicer dialog is a bad trade.
 *
 * Local members share this filesystem too, but the console can't tell local from remote
 * without the fleet list, and a leaf form field shouldn't acquire one. The fallback is
 * never WRONG — only less pleasant — so the ambiguous case takes it.
 */
export function canPickNatively(inDesktopShell: boolean, onHostConsole: boolean): boolean {
  return inDesktopShell && onHostConsole;
}

/**
 * Open the OS folder/file chooser (#2265). Three outcomes, and callers must keep them
 * apart:
 *   - `string`    — the chosen path.
 *   - `null`      — the operator CANCELLED. Leave the field as it is; a cancel is a
 *                   decision, not a failure, so it must not fall through to the in-app
 *                   browser and make the dialog feel un-dismissable.
 *   - `undefined` — no native picker here (no shell, or a shell too old to know the
 *                   command). Fall back to the in-app browser.
 */
export async function pickPathNative(opts: { start?: string; files?: boolean }): Promise<string | null | undefined> {
  const core = tauri()?.core;
  if (!core) return undefined;
  try {
    const picked = await core.invoke<string | null>("pick_path", {
      start: opts.start ?? "",
      files: Boolean(opts.files),
    });
    return typeof picked === "string" && picked ? picked : null;
  } catch {
    // An older shell without the command, or a denied capability — degrade to the
    // in-app browser rather than leaving Browse… dead. (Same posture as auth_token.)
    return undefined;
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
