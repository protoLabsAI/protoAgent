// Tenant check (pure — unit-tested without React): localStorage is origin-keyed but
// the backend behind an origin can change; on uid mismatch the previous tenant's
// persisted chat view is dropped. See app/TenantGuard.tsx for the React shell.

import { listKeys, readKey, removeKey, writeKey } from "./storage";

const UID_KEY = "protoagent.tenant.uid";
export const SWITCHED_FLAG = "protoagent.tenant.switched"; // sessionStorage — the post-reload toast

export function tenantCheck(uid: string | undefined): "ok" | "switched" {
  if (!uid) return "ok"; // older backend / unreadable root — never destructive
  // Non-throwing writes (ADR 0114 D1): this runs inside TenantGuard's effect, where a
  // throw would crash the console. The uid is not a credential.
  const stored = readKey("local", UID_KEY) || "";
  if (!stored) {
    writeKey("local", UID_KEY, uid);
    return "ok";
  }
  if (stored === uid) return "ok";

  // A different backend owns this origin now — drop the previous tenant's chat view.
  // (A prefix wipe; ADR 0114 slice 5 retires it in favour of per-uid databases.)
  listKeys("local")
    .filter((k) => k.startsWith("protoagent.chat.sessions"))
    .forEach((k) => removeKey("local", k));
  removeKey("session", "protoagent.turnwatch.notified");
  writeKey("session", SWITCHED_FLAG, "1");
  writeKey("local", UID_KEY, uid);
  return "switched";
}
