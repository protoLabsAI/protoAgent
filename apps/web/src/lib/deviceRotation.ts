/** Weekly device-token rotation (ADR 0121 D5, #4336 S7.3).
 *
 * A paired phone asks `GET /api/devices/self` on boot (and once a day while open). When the
 * server says `rotate_due`, it POSTs `/api/devices/self/rotate` and stores the new token with
 * the STRICT write (ADR 0114 D1). The server's half makes every failure here safe:
 *
 * - the old token stays valid until the NEW one is first presented, so a failed write just
 *   means "keep using the old token" — nothing is presented that wasn't stored;
 * - a second rotate presenting the old token within two minutes of an unused rotation is a
 *   409, so two tabs racing can't each mint a token and leave storage holding the loser's.
 *
 * Here: one rotation at a time per tab (a shared promise) and per browser (a Web Lock where
 * the browser has them), and the new token is written only over the token it replaces
 * (`replaceDeviceToken`). The bearer is sent explicitly and to the HUB (`host: true`): it is
 * the hub's registry that knows this device, whichever fleet member is focused. */

import { apiUrl } from "./api/routing";
import {
  cancelDeviceMove,
  completeDeviceMove,
  deviceMovePending,
  deviceToken,
  replaceDeviceToken,
  storeDeviceAddresses,
} from "./credentials";
import { notifyAuthRequired } from "./auth";

import type { DeviceSelf } from "./api/runtime";

export type RotationOutcome =
  | "not-a-device" // no device token here, or the server says this credential isn't one
  | "unsupported" // a server before S7.3 (no /api/devices/self)
  | "not-due"
  | "rotated"
  | "store-failed" // the strict write failed — still on the old (valid) token
  | "busy" // another tab is rotating, or storage changed under us
  | "moved" // this origin's token was handed off to another origin (ADR 0121 D1): forgotten here
  | "error";

/** Re-check cadence while the console stays open; rotation itself is at most weekly. */
export const ROTATION_CHECK_MS = 24 * 60 * 60 * 1000;
const LOCK_NAME = "protoagent.deviceTokenRotation";

async function call(method: "GET" | "POST", path: string, token: string): Promise<{ status: number; body: unknown }> {
  const res = await fetch(apiUrl(path, { host: true }), {
    method,
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* a non-JSON body (an old server's 404 page) */
  }
  return { status: res.status, body };
}

/** The phone's move away from this origin completed (ADR 0121 D1): forget `token` here —
 *  only if it is still the stored one (another tab may have re-paired meanwhile) — and, when
 *  no operator token is left to keep this console signed in, send it to the AuthGate. */
function finishMove(token: string): RotationOutcome {
  if (deviceToken() === token && !completeDeviceMove()) notifyAuthRequired();
  return "moved";
}

async function rotateOnce(): Promise<RotationOutcome> {
  const token = deviceToken();
  if (!token) return "not-a-device";
  const self = await call("GET", "/api/devices/self", token);
  if (self.status === 401 && deviceMovePending()) {
    // This origin recorded a move away and its token is now refused: the handed-off token's
    // grace is over (ADR 0121 D1). Forget it so the AuthGate points at the new home.
    return finishMove(token);
  }
  if (self.status === 404) return "unsupported";
  if (self.status !== 200) return "error";
  const answer = self.body as { device?: DeviceSelf | null; addresses?: unknown } | null;
  const device = answer?.device;
  if (!device) return "not-a-device";
  if (device.handed_off) {
    // The phone moved to another of the agent's origins and arrived there (ADR 0121 D1). This
    // origin's token rides out a short grace and is then refused, so stop presenting it now —
    // and never trip anything with it later. The "moved to" pointer stays for the AuthGate.
    return finishMove(token);
  }
  // Still this device's own token here: a recorded move never completed (the new address
  // didn't load, or never stored its token), so this origin is still the home.
  if (deviceMovePending()) cancelDeviceMove();
  // The same boot call refreshes the address set (ADR 0121 D1): a new public URL or tailnet
  // HTTPS reaches the phone's offline notice without re-pairing.
  storeDeviceAddresses(answer?.addresses);
  if (!device.rotate_due) return "not-due";
  const rotated = await call("POST", "/api/devices/self/rotate", token);
  if (rotated.status === 409) return "busy";
  const next = (rotated.body as { token?: unknown } | null)?.token;
  if (rotated.status !== 200 || typeof next !== "string" || !next) return "error";
  const stored = replaceDeviceToken(token, next);
  if (stored === "replaced") return "rotated";
  return stored === "store-failed" ? "store-failed" : "busy";
}

async function withBrowserLock(fn: () => Promise<RotationOutcome>): Promise<RotationOutcome> {
  const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
  if (!locks?.request) return fn();
  // ifAvailable: a tab that finds another tab rotating just skips — that tab's write lands in
  // the same localStorage this one reads per request.
  return locks.request(LOCK_NAME, { ifAvailable: true }, (lock) => (lock ? fn() : "busy"));
}

let inflight: Promise<RotationOutcome> | null = null;

/** Rotate this browser's device token if the server says it's due. Never throws. */
export function rotateDeviceTokenIfDue(): Promise<RotationOutcome> {
  if (!inflight) {
    inflight = withBrowserLock(rotateOnce)
      .catch((): RotationOutcome => "error")
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

/** Boot hook: check now, then once a day while the console is open. Returns a stop function. */
export function startDeviceTokenRotation(): () => void {
  // Without a device token this returns at once, no request — and the daily tick stays
  // armed because the browser may be paired later in this tab.
  void rotateDeviceTokenIfDue();
  const timer = setInterval(() => void rotateDeviceTokenIfDue(), ROTATION_CHECK_MS);
  return () => clearInterval(timer);
}
