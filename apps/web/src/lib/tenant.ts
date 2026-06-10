// Tenant check (pure — unit-tested without React): localStorage is origin-keyed but
// the backend behind an origin can change; on uid mismatch the previous tenant's
// persisted chat view is dropped. See app/TenantGuard.tsx for the React shell.

const UID_KEY = "protoagent.tenant.uid";
export const SWITCHED_FLAG = "protoagent.tenant.switched"; // sessionStorage — the post-reload toast

export function tenantCheck(uid: string | undefined): "ok" | "switched" {
  if (!uid) return "ok"; // older backend / unreadable root — never destructive
  let stored = "";
  try {
    stored = localStorage.getItem(UID_KEY) || "";
  } catch {
    return "ok";
  }
  if (!stored) {
    localStorage.setItem(UID_KEY, uid);
    return "ok";
  }
  if (stored === uid) return "ok";

  // A different backend owns this origin now — drop the previous tenant's chat view.
  const stale: string[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i) || "";
    if (k.startsWith("protoagent.chat.sessions")) stale.push(k);
  }
  stale.forEach((k) => localStorage.removeItem(k));
  try {
    sessionStorage.removeItem("protoagent.turnwatch.notified");
    sessionStorage.setItem(SWITCHED_FLAG, "1");
  } catch {
    /* best-effort */
  }
  localStorage.setItem(UID_KEY, uid);
  return "switched";
}

