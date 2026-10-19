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

import { readKey, removeKey, writeKey, writeKeyStrict } from "./storage";

export const OPERATOR_TOKEN_KEY = "protoagent.authToken";
export const DEVICE_TOKEN_KEY = "protoagent.deviceToken";
export const DEVICE_ID_KEY = "protoagent.deviceId";
/** This device's ADDRESS SET (ADR 0121 D1, #4336 S7.5): the agent's origins, best first, as
 *  the claim and `GET /api/devices/self` report them. localStorage is per origin, so this is
 *  stored per HOME origin by construction — next to the token it belongs to. */
export const DEVICE_ADDRESSES_KEY = "protoagent.deviceAddresses";
/** Where this phone moved to (a handoff, ADR 0121 D1): written on the OLD origin just before
 *  navigating, so when that origin is opened again it can point at the new home. */
export const DEVICE_MOVED_KEY = "protoagent.deviceMovedTo";
/** Fired on `window` when this tab swaps in a rotated device token, so anything holding a
 *  copy of the bearer (an open plugin frame) can pick up the new one. */
export const CREDENTIAL_CHANGED_EVENT = "protoagent:credential";

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
  // Paired (or moved) HERE: this origin is the home now, so an old "moved to" pointer is stale.
  removeKey("local", DEVICE_MOVED_KEY);
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
  removeKey("local", DEVICE_ADDRESSES_KEY);
  return read(OPERATOR_TOKEN_KEY) !== "";
}

/** One of the agent's origins, as the server lists it. `requirement` is what the phone needs
 *  for it to work (`tailscale` / `same_network`); a public origin has none. */
export type DeviceAddress = {
  kind: "tailnet_https" | "public" | "tailnet" | "lan";
  origin: string;
  requirement?: "tailscale" | "same_network";
};

const ADDRESS_KINDS = new Set(["tailnet_https", "public", "tailnet", "lan"]);
const REQUIREMENTS = new Set(["tailscale", "same_network"]);

/** The usable entries of a server's `addresses` (unknown kinds, non-http(s) or non-origin
 *  strings dropped), in the server's order. Never throws. */
export function parseDeviceAddresses(raw: unknown): DeviceAddress[] {
  if (!Array.isArray(raw)) return [];
  const out: DeviceAddress[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const { kind, origin, requirement } = item as Record<string, unknown>;
    if (typeof kind !== "string" || !ADDRESS_KINDS.has(kind) || typeof origin !== "string") continue;
    let url: URL;
    try {
      url = new URL(origin);
    } catch {
      continue;
    }
    // An ORIGIN only — this string is later navigated to, so nothing else gets through.
    if ((url.protocol !== "http:" && url.protocol !== "https:") || url.origin !== origin) continue;
    const entry: DeviceAddress = { kind: kind as DeviceAddress["kind"], origin };
    if (typeof requirement === "string" && REQUIREMENTS.has(requirement)) {
      entry.requirement = requirement as DeviceAddress["requirement"];
    }
    out.push(entry);
  }
  return out;
}

/** Store the address set the server just reported. Best-effort (a plain write): losing it only
 *  costs the offline notice its links until the next boot refreshes it. An answer with no
 *  usable entry leaves the stored set alone. */
export function storeDeviceAddresses(raw: unknown): void {
  const list = parseDeviceAddresses(raw);
  if (!list.length) return;
  try {
    writeKey("local", DEVICE_ADDRESSES_KEY, JSON.stringify(list));
  } catch {
    /* best-effort */
  }
}

/** This device's stored address set, or [] when it isn't a paired device here. */
export function deviceAddresses(): DeviceAddress[] {
  if (!read(DEVICE_TOKEN_KEY)) return [];
  try {
    return parseDeviceAddresses(JSON.parse(read(DEVICE_ADDRESSES_KEY) || "[]"));
  } catch {
    return [];
  }
}

/** The address set minus THIS origin (the home): where else this phone could go. */
export function otherDeviceAddresses(home: string = currentOrigin()): DeviceAddress[] {
  return deviceAddresses().filter((a) => a.origin !== home);
}

function currentOrigin(): string {
  try {
    return window.location.origin;
  } catch {
    return "";
  }
}

/** Record that this phone is moving to `origin` (written on the OLD origin before leaving). */
export function markDeviceMoved(origin: string): void {
  try {
    writeKey("local", DEVICE_MOVED_KEY, origin);
  } catch {
    /* best-effort: it only powers the "this phone moved" pointer */
  }
}

/** Where this phone moved to, or "" — only once it no longer holds a device token here. */
export function deviceMovedTo(): string {
  if (read(DEVICE_TOKEN_KEY)) return "";
  const origin = read(DEVICE_MOVED_KEY);
  return parseDeviceAddresses([{ kind: "lan", origin }]).length ? origin : "";
}

/** Whether this origin recorded a move away (`markDeviceMoved`), whatever it still holds. */
export function deviceMovePending(): boolean {
  return read(DEVICE_MOVED_KEY) !== "";
}

/** The move didn't happen (this origin's token is still the device's own): drop the pointer. */
export function cancelDeviceMove(): void {
  removeKey("local", DEVICE_MOVED_KEY);
}

/** The move finished (the server says this origin's token was handed off): forget the token
 *  HERE so it is never presented again, and keep the pointer to the new home. Returns true
 *  when an operator token remains. */
export function completeDeviceMove(): boolean {
  return forgetDeviceCredential();
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

/** This browser's device token, or "" when it isn't a paired device. */
export function deviceToken(): string {
  return read(DEVICE_TOKEN_KEY);
}

export type ReplaceResult = "replaced" | "store-failed" | "changed";

/** Swap in a ROTATED device token (ADR 0121 D5) — strictly, and only over the token it
 *  replaces.
 *
 *  - `store-failed`: the strict write threw, so the stored (old) token is untouched and is
 *    what the next request sends. That is safe by design: the server keeps the old token
 *    valid until the NEW one is first presented, and a token this browser never stored is
 *    never presented.
 *  - `changed`: storage no longer holds `previous` (another tab rotated, or the device was
 *    re-paired meanwhile). Writing now would clobber a newer token, so nothing is written.
 *  - `replaced`: stored. `effectiveToken()` reads storage per call, so the very next request
 *    carries the new token. */
export function replaceDeviceToken(previous: string, next: string): ReplaceResult {
  if (!previous || read(DEVICE_TOKEN_KEY) !== previous) return "changed";
  try {
    writeKeyStrict("local", DEVICE_TOKEN_KEY, next);
  } catch {
    return "store-failed";
  }
  try {
    window.dispatchEvent(new Event(CREDENTIAL_CHANGED_EVENT));
  } catch {
    /* no window (a worker) — nothing to tell */
  }
  return "replaced";
}
