import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { SWITCHED_FLAG, tenantCheck } from "../lib/tenant";

// Tenant guard: localStorage is keyed by ORIGIN, but the backend behind an origin can
// change (a port handed from one agent to another — e.g. a fork booted on the old
// port). The chat store persists full transcripts client-side, so without a check the
// new tenant's window renders the PREVIOUS agent's conversations. The server exposes a
// stable per-data-root uid (runtime-status `instance_uid`); on mismatch we drop the
// previous tenant's chat view (all slugs) + the turn-watcher state, then reload so the
// already-hydrated stores restart clean. Layout/theme/authToken stay — layout is
// cosmetic, theme is server-side per agent, and clearing a bearer token could lock the
// operator out. Same data root ⇒ same uid, so restarts/upgrades of the SAME agent
// never trip this.

export function TenantGuard({ uid }: { uid: string | undefined }) {
  const toast = useToast();

  // Post-reload notice — the clearing happened just before the reload below.
  useEffect(() => {
    try {
      if (sessionStorage.getItem(SWITCHED_FLAG)) {
        sessionStorage.removeItem(SWITCHED_FLAG);
        toast({
          tone: "info",
          title: "Different agent on this address",
          message: "The previous agent's chat view was cleared — its data is untouched on its own instance.",
        });
      }
    } catch {
      /* best-effort */
    }
  }, [toast]);

  useEffect(() => {
    if (tenantCheck(uid) === "switched") {
      // The chat store hydrated from the stale keys at module init — reload to restart
      // clean (a one-time event, only on an actual tenant switch).
      window.location.reload();
    }
  }, [uid]);

  return null;
}
