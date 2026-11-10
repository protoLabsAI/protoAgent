import { useQuery } from "@tanstack/react-query";
import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { api, currentSlug } from "../lib/api";
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
export function evictionToast(report: EvictionReport, here: string = currentSlug()): { title: string; message: string } {
  const title = "Freed up browser storage";
  const slugs = report.evicted.map((e) => e.slug);
  // IndexedDB eviction (epic #4981 S4) can reach this agent's own oldest chats, and palette threads.
  if (slugs.length && slugs.every((slug) => slug === here)) {
    return {
      title,
      message:
        slugs.length === 1
          ? "Cleared your oldest saved chat in this browser so new chats keep saving."
          : `Cleared your ${slugs.length} oldest saved chats in this browser so new chats keep saving.`,
    };
  }
  if (slugs.length && slugs.every((slug) => slug === "palette")) {
    return { title, message: "Cleared older quick-chat and direct-message history so new chats keep saving." };
  }
  const agents = new Set(slugs.filter((slug) => slug !== "palette" && slug !== here)).size;
  return {
    title,
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

  // Free up space (Settings → Storage) says what it cleared itself.
  useEffect(
    () =>
      subscribeEvictions((report) => {
        if (report.reason !== "free") toast({ tone: "info", ...evictionToast(report) });
      }),
    [toast],
  );

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
