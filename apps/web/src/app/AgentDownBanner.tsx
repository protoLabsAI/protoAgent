import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Banner, Button } from "@protolabsai/ui/primitives";

import { api, agentHref, currentSlug } from "../lib/api";
import { brandName } from "../lib/brand";
import { fleetQuery, queryKeys } from "../lib/queries";

/** Runtime down-detection for the FOCUSED fleet member.
 *
 *  The boot gate (App.tsx) only fires while the runtime probe is still failing at
 *  BOOT; once an agent has loaded, its runtime query stops polling (graph_loaded),
 *  so a member that stops MID-SESSION left every plugin view to 409 with a raw
 *  "Could not load: … agent '<slug>' is not running" and gave the operator no
 *  app-level warning that the agent they were watching had gone down.
 *
 *  This hangs off the fleet status the FleetSwitcher already polls (queryKeys.fleet,
 *  3s — no extra traffic): when the agent THIS window is on has flipped to
 *  not-running, it renders a slim warning strip with a hub-routed Start. `startAgent`
 *  is a hub control-plane call (`/api/fleet/<name>/start`, never slug-scoped), so it
 *  works even though the focused member is down — "going back to the main instance".
 *  A REMOTE member has no local process to start from here, so it points back to the
 *  host instead. The host window, or a running agent, renders nothing. */
export function AgentDownBanner() {
  const slug = currentSlug();
  const qc = useQueryClient();
  const [starting, setStarting] = useState(false);
  const fleet = useQuery(fleetQuery());

  if (slug === "host") return null;
  const agent = (fleet.data?.agents ?? []).find((a) => (a.host ? "host" : a.id) === slug);
  if (!agent || agent.host || agent.running) return null;

  const start = () => {
    void (async () => {
      setStarting(true);
      try {
        await api.startAgent(agent.name);
        // On success the next 3s poll reports running:true and this banner self-hides;
        // invalidate so it flips without waiting out the interval.
        await qc.invalidateQueries({ queryKey: queryKeys.fleet });
      } catch {
        // Leave the banner up — the poll keeps its state honest and the operator can retry.
      } finally {
        setStarting(false);
      }
    })();
  };

  return (
    <Banner
      tone="warning"
      title="agent stopped"
      className="shell-warning-banner agent-down-banner"
      action={
        agent.remote ? (
          <Button
            size="sm"
            variant="primary"
            onClick={() => {
              window.location.href = agentHref("host");
            }}
          >
            Return to host
          </Button>
        ) : (
          <Button size="sm" variant="primary" loading={starting} onClick={start}>
            Start
          </Button>
        )
      }
    >
      {brandName(agent.name)} has stopped —{" "}
      {agent.remote
        ? "it’s unreachable from here; return to the host console."
        : "start it to reload this view."}
    </Banner>
  );
}
