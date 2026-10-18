/** Where this browser keeps its bearer(s) — and which one it sends (ADR 0087, #4336 S3).
 *
 * Two credentials, two keys:
 *
 * - `protoagent.authToken` — the OPERATOR token: the shared bearer someone typed into the
 *   AuthGate (or the desktop shell handed over, or the Devices "make reachable" flow minted).
 * - `protoagent.deviceToken` — a per-DEVICE token from a pairing claim (QR or typed code),
 *   with `protoagent.deviceId` naming the registry row it belongs to.
 *
 * They used to share `protoagent.authToken`, so pairing a browser that already held the
 * operator token silently overwrote it, and the console couldn't tell which kind it held.
 *
 * **Which one is sent:** the device token wins when present. It is the narrower, individually
 * revocable credential (and, after ADR 0121 D4, the scoped one), and a browser only has one
 * because someone deliberately paired it. A dead device token can't strand the operator:
 * saving a token in the AuthGate drops it (`lib/auth.ts`), and removing this device from
 * Settings ▸ Devices forgets it (`forgetDeviceCredential`), falling back to the operator
 * token if one is kept.
 *
 * Leaf module (imports only the storage seam) so `api/routing.ts`, `auth.ts` and `pairing.ts`
 * can all depend on it without a cycle. */

import { readKey, removeKey, writeKeyStrict } from "./storage";

export const OPERATOR_TOKEN_KEY = "protoagent.authToken";
export const DEVICE_TOKEN_KEY = "protoagent.deviceToken";
export const DEVICE_ID_KEY = "protoagent.deviceId";

function read(key: string): string {
  try {
    return readKey("local", key) || "";
  } catch {
    return "";
  }
}

/** The bearer this console sends: the device token if paired, else the operator token. */
export function effectiveToken(): string {
  return read(DEVICE_TOKEN_KEY) || read(OPERATOR_TOKEN_KEY);
}

/** The device-registry id of THIS browser's pairing, or "" when it isn't a paired device.
 *  Only meaningful together with a device token: an id whose token is gone names nothing. */
export function currentDeviceId(): string {
  return read(DEVICE_TOKEN_KEY) ? read(DEVICE_ID_KEY) : "";
}

/** Store a freshly claimed device token. STRICT for the token (ADR 0114 D1): a token the
 *  browser failed to keep must not report success. The id is informational (it drives the
 *  "This device" mark), so a failed id write is tolerated — and an old id is cleared first so
 *  it can never be paired with the NEW token. */
export function storeDeviceCredential(token: string, deviceId: string | undefined): void {
  removeKey("local", DEVICE_ID_KEY);
  writeKeyStrict("local", DEVICE_TOKEN_KEY, token);
  if (deviceId) {
    try {
      writeKeyStrict("local", DEVICE_ID_KEY, deviceId);
    } catch {
      /* informational — the device still works, it just isn't marked "This device" */
    }
  }
}

/** Forget this browser's device credential (it was revoked, or the operator replaced it).
 *  Returns true when an operator token remains, i.e. the console is STILL signed in. */
export function forgetDeviceCredential(): boolean {
  removeKey("local", DEVICE_TOKEN_KEY);
  removeKey("local", DEVICE_ID_KEY);
  return read(OPERATOR_TOKEN_KEY) !== "";
}

export type MigrationResult = "migrated" | "nothing-to-do" | "kept-legacy";

/** One-shot, idempotent move of a pre-S3 phone's token to its own key.
 *
 * Before S3 a claim wrote the device token into `protoagent.authToken` and the id into
 * `protoagent.deviceId`. A browser with BOTH (and no `protoagent.deviceToken` yet) is such a
 * phone. Order matters for "nobody gets logged out": copy strictly first, and only remove the
 * old key once the copy is known to be stored. If the copy fails, nothing moves — the old key
 * keeps authenticating exactly as before (`effectiveToken` reads it) and the next boot retries.
 *
 * Known imprecision: a browser that typed the shared bearer into the AuthGate AFTER pairing
 * (pre-S3, which kept the stale id) has that bearer filed as its device token. It keeps
 * authenticating — `effectiveToken` sends it either way — and its id names a revoked device,
 * so no row is marked "This device". Telling the two apart would need a server round-trip
 * before first render, for a cosmetic difference. */
export function migrateLegacyDeviceToken(): MigrationResult {
  const legacy = read(OPERATOR_TOKEN_KEY);
  if (!legacy || !read(DEVICE_ID_KEY) || read(DEVICE_TOKEN_KEY)) return "nothing-to-do";
  try {
    writeKeyStrict("local", DEVICE_TOKEN_KEY, legacy);
  } catch {
    return "kept-legacy";
  }
  removeKey("local", OPERATOR_TOKEN_KEY);
  return "migrated";
}
