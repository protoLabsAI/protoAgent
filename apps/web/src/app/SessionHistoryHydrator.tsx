import { useEffect } from "react";

import { hydrateDurableChatSessions } from "../chat/sessionHydration";

let started = false;

/** Boot-time, renderless recovery. Wait for the hub tenant uid so TenantGuard
 * gets the first chance to clear origin-local history after an address changes
 * owners; then hydrate this focused agent exactly once per page load. */
export function SessionHistoryHydrator({ tenantUid }: { tenantUid: string | undefined }) {
  useEffect(() => {
    if (!tenantUid || started) return;
    started = true;
    void hydrateDurableChatSessions();
  }, [tenantUid]);
  return null;
}
