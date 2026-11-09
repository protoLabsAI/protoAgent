import { useQuery } from "@tanstack/react-query";
import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import {
  enforceStorageBudget,
  setFleetSlugs,
  subscribeEvictions,
  type EvictionReport,
} from "../lib/storageEviction";

/** How long after mount the once-a-session budget check waits for the fleet list. */
export const BUDGET_CHECK_DELAY_MS = 10_000;

let budgetChecked = false;

/** The toast copy for an eviction: what happened, in the operator's terms. */
export function evictionToast(report: EvictionReport): { title: string; message: string } {
  const agents = new Set(report.evicted.map((e) => e.slug)).size;
  return {
    title: "Freed up browser storage",
    message:
      agents === 1
        ? "Cleared the saved chat history of an agent you haven't opened lately, so new chats keep saving."
        : `Cleared the saved chat history of ${agents} agents you haven't opened lately, so new chats keep saving.`,
  };
}

/** Renderless: feed eviction the fleet list (so it knows which agents have left), run the
 *  budget check once a session, and toast whenever eviction cleared something. */
export function StorageUpkeep() {
  const toast = useToast();
  const fleet = useQuery({ queryKey: queryKeys.fleet, queryFn: () => api.fleet(), retry: false });

  useEffect(() => subscribeEvictions((report) => toast({ tone: "info", ...evictionToast(report) })), [toast]);

  // Only a full read (one that includes this instance) counts; anything else is "unknown".
  const agents = fleet.data?.agents;
  const complete = Boolean(agents?.some((agent) => agent.host));
  useEffect(() => {
    setFleetSlugs(complete && agents ? agents.map((agent) => (agent.host ? "host" : agent.id)) : null);
  }, [agents, complete]);

  useEffect(() => {
    if (budgetChecked) return;
    const run = () => {
      if (budgetChecked) return;
      budgetChecked = true;
      enforceStorageBudget();
    };
    if (complete || fleet.isError) {
      run();
      return;
    }
    const timer = window.setTimeout(run, BUDGET_CHECK_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [complete, fleet.isError]);

  return null;
}
