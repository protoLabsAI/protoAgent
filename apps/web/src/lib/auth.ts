// 401-driven auth state (#873) — a tiny subscribable store (the chat-store
// pattern; no zustand needed for one boolean). `request()` and the A2A stream
// fetch call notifyAuthRequired() on a 401; the AuthGate dialog subscribes and
// prompts for the operator bearer. Pure module so it's unit-testable and usable
// from non-React code (api.ts).

// SECURITY — the bearer is cached here in localStorage, an ACCEPTED residual: a
// console-origin XSS could read it. The credential's real home is the server env
// (`A2A_AUTH_TOKEN`); this is just the browser's copy so it can authenticate. We do NOT
// try to "harden" it in place — an httpOnly cookie can't auth the cross-origin desktop
// webview, and hashing/encrypting at rest is no defense against same-origin XSS (a script
// in the page can read the key and reuse this very send path). Bounded by the
// localhost-default + default-deny bearer-gate posture; the real lever beyond localhost is
// a CSP connect-src egress limit. See docs/guides/deploy-docker.md → "Where the operator
// token lives".
import { effectiveToken, forgetDeviceCredential, OPERATOR_TOKEN_KEY } from "./credentials";
import { removeKeyStrict, writeKeyStrict } from "./storage";

const TOKEN_KEY = OPERATOR_TOKEN_KEY;

let needed = false;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function authRequired(): boolean {
  return needed;
}

/** Flip the "needs auth" state (idempotent — panels can 401 in bursts).
 *
 * In the desktop app, try the shell's own token FIRST (#2055): it spawned the server and
 * knows the secret, so prompting the operator for it is asking them to look up something
 * their own machine already has. Only if that yields nothing does the gate go up.
 *
 * `sentAuthorization` is the `Authorization` header the 401'd request carried (null when it
 * sent none). A 401 for a credential that has since been REPLACED is stale, not a reason to
 * prompt: requests already in flight when the token changed (a desktop self-auth, a Connect in
 * the AuthGate) come back 401 after the new token is stored, and used to put the gate straight
 * back up over a console that was already signed in (#5408). Omitted = always counts. */
export function notifyAuthRequired(sentAuthorization?: string | null) {
  if (sentAuthorization !== undefined) {
    const current = effectiveToken();
    if ((sentAuthorization || "") !== (current ? `Bearer ${current}` : "")) return;
  }
  if (needed) return;
  void tryDesktopSelfAuth();
  needed = true;
  emit();
}

/** Dismiss without saving (the operator chose "Not now"); the next 401 re-prompts. */
export function clearAuthRequired() {
  if (!needed) return;
  needed = false;
  emit();
}

/** Persist the operator bearer (the key api.ts's authToken() falls back to) and clear the
 *  prompt. A credential write is STRICT (ADR 0114 D1): when the browser can't keep the
 *  token (full quota, storage disabled) every request would still go out without it, so
 *  this reports the failure and leaves the prompt up instead of pretending it connected.
 *
 *  Also FORGETS a paired-device token (`lib/credentials.ts`). The device token is sent in
 *  preference to the operator token, so if one is held it is the credential that just
 *  401'd — leaving it in place would keep shadowing the token being saved, and the gate
 *  would come straight back. */
export function saveAuthToken(token: string): { ok: true } | { ok: false; error: string } {
  try {
    const t = token.trim();
    if (t) writeKeyStrict("local", TOKEN_KEY, t);
    else removeKeyStrict("local", TOKEN_KEY);
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "could not save the token" };
  }
  forgetDeviceCredential();
  clearAuthRequired();
  return { ok: true };
}


/** True once a desktop self-auth attempt has run — one shot per page, so a burst of 401s
 *  doesn't fire a storm of IPC calls, and a genuinely-wrong token still reaches the prompt. */
let desktopAuthTried = false;

async function tryDesktopSelfAuth(): Promise<void> {
  if (desktopAuthTried) return;
  desktopAuthTried = true;
  const { desktopAuthToken } = await import("./desktop");
  const token = await desktopAuthToken();
  // Only useful if it differs from what we already sent — otherwise the server rejected
  // this exact token and re-saving it would loop.
  // An unreadable store reads as "" — we just try the shell's token.
  const existing = effectiveToken();
  if (!token || token === existing) return;
  // A token the browser couldn't keep recovers nothing — leave the prompt to say so.
  if (!saveAuthToken(token).ok) return;
  // Same recovery the manual prompt does (AuthGate's connect/pair): refetch every query with the
  // new bearer. A 401 never retries (queryClient.ts), so without this every panel that 401'd
  // stayed errored after the gate went away (#5408). This used to dispatch a
  // `protoagent:auth-recovered` event that nothing listened for. Imported lazily: queryClient
  // imports api, which imports this module.
  const { queryClient } = await import("./queryClient");
  void queryClient.invalidateQueries();
}
