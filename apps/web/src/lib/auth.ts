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
const TOKEN_KEY = "protoagent.authToken";

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
 * their own machine already has. Only if that yields nothing does the gate go up. */
export function notifyAuthRequired() {
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

/** Persist the operator bearer (the key api.ts's authToken() reads) and clear the
 *  prompt. Storage can be unavailable in hardened contexts — the token still won't
 *  survive a reload there, but in-flight retries pick it up via the gate's refetch. */
export function saveAuthToken(token: string) {
  try {
    const t = token.trim();
    if (t) window.localStorage.setItem(TOKEN_KEY, t);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // best-effort
  }
  clearAuthRequired();
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
  let existing = "";
  try {
    existing = window.localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    // best-effort; an unreadable store just means we try the shell's token
  }
  if (!token || token === existing) return;
  saveAuthToken(token);
  // Same recovery path the manual prompt takes: everything refetches with the new bearer.
  window.dispatchEvent(new CustomEvent("protoagent:auth-recovered"));
}
