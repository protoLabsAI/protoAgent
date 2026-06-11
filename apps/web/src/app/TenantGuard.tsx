import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { SWITCHED_FLAG, tenantCheck } from "../lib/tenant";

// Tenant guard: localStorage is keyed by ORIGIN, but the backend behind an origin can
// change (a fork booted on the old port — a different data root now answers here). The
// chat store persists full transcripts client-side, so without a check the new tenant's
// window renders the PREVIOUS one's conversations. The `uid` here is the HUB's stable
// per-data-root uid (host-pinned runtime status, NOT the focused agent's — switching
// fleet agents keeps the same hub, so a normal swap must NOT trip this). On mismatch we
// drop the persisted chat view (all slugs) + the turn-watcher state, then reload so the
// already-hydrated stores restart clean. Layout/theme/authToken stay — layout is
// cosmetic, theme is server-side per agent, and clearing a bearer token could lock the
// operator out. Same hub data root ⇒ same uid, so restarts/upgrades + agent swaps
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
