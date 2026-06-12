// 401-driven auth state (#873) — a tiny subscribable store (the chat-store
// pattern; no zustand needed for one boolean). `request()` and the A2A stream
// fetch call notifyAuthRequired() on a 401; the AuthGate dialog subscribes and
// prompts for the operator bearer. Pure module so it's unit-testable and usable
// from non-React code (api.ts).

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

/** Flip the "needs auth" state (idempotent — panels can 401 in bursts). */
export function notifyAuthRequired() {
  if (needed) return;
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
