import { useEffect } from "react";

import { resyncDurableChatSessions, watchDurableSessions } from "../chat/sessionHydration";

let started = false;

/** Renderless durable-history sync. Wait for the hub tenant uid so TenantGuard gets the
 * first chance to clear origin-local history after an address changes owners; then
 * hydrate this focused agent once at boot, and keep re-syncing while the console stays
 * open — on return to the window and on a slow interval while visible — so turns another
 * device runs show up without a reload (ADR 0104). */
export function SessionHistoryHydrator({ tenantUid }: { tenantUid: string | undefined }) {
  useEffect(() => {
    if (!tenantUid || started) return;
    started = true;
    void resyncDurableChatSessions({ force: true });
  }, [tenantUid]);
  useEffect(() => {
    if (!tenantUid) return;
    return watchDurableSessions();
  }, [tenantUid]);
  return null;
}
