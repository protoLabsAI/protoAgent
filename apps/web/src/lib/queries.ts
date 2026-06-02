import { queryOptions } from "@tanstack/react-query";

import { api } from "./api";

// Centralized query keys + option factories (ADR 0013). Surfaces read these via
// `useSuspenseQuery(...)`; mutations invalidate the matching key. Keep keys
// stable and hierarchical so a mutation can invalidate a whole subtree.
export const queryKeys = {
  goals: ["goals"] as const,
};

// Goals the agent works toward (goal mode). Lives in the right sidebar and
// refetches every 5s while mounted — the agent advances/clears goals mid-turn,
// so the panel should track that without a manual refresh.
export const goalsQuery = () =>
  queryOptions({
    queryKey: queryKeys.goals,
    queryFn: () => api.goals(),
    refetchInterval: 5_000,
  });
