import type { QueryClient } from "@tanstack/react-query";

/**
 * Post-setup cache convergence (#2462). Finishing the wizard rewires the whole
 * backend — config, provider/model, plugins, tools, graph — so every
 * server-derived query is stale by construction at that moment. Blanket
 * invalidation (deliberately NO filter): active queries (runtime, settings,
 * models) refetch immediately, inactive ones on next mount, and the call can't
 * drift when a new query is added — the exact failure mode that produced the
 * stale composer chip.
 */
export function invalidateAllAfterSetup(queryClient: QueryClient): void {
  void queryClient.invalidateQueries();
}
