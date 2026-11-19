/** Browser push for the existing paired-device credential plane (ADR 0135 W2).
 * No credentials or subscription secrets are persisted outside the existing stores.
 */
import { apiUrl } from "./api/routing";
import { deviceToken } from "./credentials";
import { readKey, writeKey } from "./storage";

export type PushState = "unsupported" | "denied" | "not enrolled" | "enrolled";
type Status = { public_key: string; version: number; binding: { vapid_version: number } | null; renew: string | null };
const DISABLED = "protoagent.pushDisabled";

function supported(): boolean {
  return typeof window !== "undefined" && window.isSecureContext === true &&
    "serviceWorker" in navigator && typeof PushManager !== "undefined" && typeof Notification !== "undefined";
}

/** Safe at startup: registration never asks for notification permission. */
export async function registerPushWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!supported()) return null;
  return navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" });
}

// Serialize browser subscription changes, including across tabs where Web Locks exist.
let pending: Promise<unknown> = Promise.resolve();
function exclusive<T>(action: () => Promise<T>): Promise<T> {
  const run = async (): Promise<T> => navigator.locks?.request ? await navigator.locks.request("protoagent.push", action) : await action();
  const next = pending.then(run, run);
  pending = next.catch(() => {});
  return next;
}

async function call(method: string, token: string, body?: unknown): Promise<Status> {
  const res = await fetch(apiUrl("/api/devices/self/push", { host: true }), {
    method,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    cache: "no-store",
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`Push enrollment: HTTP ${res.status}`);
  return res.json();
}

function applicationKey(key: string): Uint8Array<ArrayBuffer> {
  const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function matches(subscription: PushSubscription, key: Uint8Array): boolean {
  const current = subscription.options.applicationServerKey;
  return !!current && new Uint8Array(current).length === key.length &&
    new Uint8Array(current).every((byte, i) => byte === key[i]);
}

async function activeWorker(): Promise<ServiceWorkerRegistration> {
  const registration = await registerPushWorker();
  if (!registration) throw new Error("Push unsupported");
  if (registration.active) return registration;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("Push worker activation timed out")), 10_000); }),
    ]);
  } finally { clearTimeout(timer); }
}

/** Read authoritative enrollment; network failures reject rather than report success. */
export async function pushState(): Promise<PushState> {
  if (!supported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  const token = deviceToken();
  if (!token || Notification.permission !== "granted" || readKey("local", DISABLED) === "true") return "not enrolled";
  const registration = await navigator.serviceWorker.getRegistration("/app/");
  const subscription = await registration?.pushManager.getSubscription();
  if (!subscription || (subscription.expirationTime !== null && subscription.expirationTime <= Date.now())) return "not enrolled";
  const status = await call("GET", token);
  return status.binding && status.binding.vapid_version === status.version && matches(subscription, applicationKey(status.public_key))
    ? "enrolled" : "not enrolled";
}

/** Renew without prompting. Re-PUT even an unchanged subscription to reconcile hub state. */
export function renewPush(): Promise<PushState> {
  if (!supported()) return Promise.resolve("unsupported");
  if (Notification.permission === "denied") return Promise.resolve("denied");
  if (Notification.permission !== "granted" || !deviceToken() || readKey("local", DISABLED) === "true") {
    return Promise.resolve("not enrolled");
  }
  return exclusive(async () => {
    const token = deviceToken();
    if (!token || Notification.permission !== "granted" || readKey("local", DISABLED) === "true") return "not enrolled";
    const status = await call("GET", token);
    const key = applicationKey(status.public_key);
    const registration = await activeWorker();
    let subscription = await registration.pushManager.getSubscription();
    if (subscription && (!status.binding || status.binding.vapid_version !== status.version ||
        !matches(subscription, key) || (subscription.expirationTime !== null && subscription.expirationTime <= Date.now()))) {
      await subscription.unsubscribe();
      subscription = null;
    }
    if (deviceToken() !== token) return "not enrolled";
    subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    // Never send a retired credential after a concurrent pairing/rotation in this tab.
    if (deviceToken() !== token) return "not enrolled";
    await call("PUT", token, { ...subscription.toJSON(), vapid_version: status.version });
    return "enrolled";
  });
}

/** W2e must call this directly from a user gesture, before any asynchronous work. */
export async function enrollPush(): Promise<PushState> {
  if (!supported()) return "unsupported";
  if (!deviceToken()) return "not enrolled";
  const permission = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
  if (permission !== "granted") return permission === "denied" ? "denied" : "not enrolled";
  writeKey("local", DISABLED, "false");
  return renewPush();
}

/** Remove only this origin's browser subscription after a confirmed handoff. The hub
 * already retired the old binding; using the retired token to DELETE could target the new home. */
export async function removeLocalPushSubscription(expectedToken = deviceToken()): Promise<void> {
  if (!supported()) return;
  await exclusive(async () => {
    const registration = await navigator.serviceWorker.getRegistration("/app/");
    const subscription = await registration?.pushManager.getSubscription();
    if (deviceToken() === expectedToken) await subscription?.unsubscribe();
  });
}

export async function unenrollPush(): Promise<void> {
  writeKey("local", DISABLED, "true");
  if (!supported()) return;
  await exclusive(async () => {
    const token = deviceToken();
    try { if (token) await call("DELETE", token); }
    finally {
      const registration = await navigator.serviceWorker.getRegistration("/app/");
      await (await registration?.pushManager.getSubscription())?.unsubscribe();
    }
  });
}

/** Boot is best effort; a later authenticated visit retries network/activation failures. */
export async function startPush(credentialsReady: Promise<unknown> = Promise.resolve()): Promise<void> {
  try {
    await registerPushWorker();
    await credentialsReady;
    if (supported() && Notification.permission === "granted") await renewPush();
  } catch { /* Push must never prevent the console from opening. */ }
}
