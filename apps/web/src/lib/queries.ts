import { queryOptions } from "@tanstack/react-query";

import { api } from "./api";

// Centralized query keys + option factories (ADR 0013). Surfaces read these via
// `useSuspenseQuery(...)`; mutations invalidate the matching key. Keep keys
// stable and hierarchical so a mutation can invalidate a whole subtree.
export const queryKeys = {
  goals: ["goals"] as const,
  beadsIssues: ["beads", "issues"] as const,
  workflows: ["workflows"] as const,
  subagents: ["subagents"] as const,
  telemetry: ["telemetry"] as const,
  settings: ["settings", "schema"] as const,
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

// The agent's task board (in-process beads store — always available). Refetches
// while mounted so the panel tracks issues the agent files/closes mid-turn.
export const beadsIssuesQuery = () =>
  queryOptions({
    queryKey: queryKeys.beadsIssues,
    queryFn: () => api.beadsIssues(),
    refetchInterval: 5_000,
  });

// Registered workflow recipes + the subagent registry — config, not live, so no
// poll; invalidated when the agent/console saves or deletes one.
export const workflowsQuery = () =>
  queryOptions({
    queryKey: queryKeys.workflows,
    queryFn: () => api.workflows(),
  });

export const subagentsQuery = () =>
  queryOptions({
    queryKey: queryKeys.subagents,
    queryFn: () => api.subagents(),
  });

// Telemetry dashboard (ADR 0006) — the summary + recent turns + insights in one
// read (mirrors the surface's original Promise.all). Refreshed by invalidation.
export const telemetryQuery = () =>
  queryOptions({
    queryKey: queryKeys.telemetry,
    queryFn: async () => {
      const [s, r, i] = await Promise.all([
        api.telemetrySummary(),
        api.telemetryRecent(50),
        api.telemetryInsights(),
      ]);
      return {
        enabled: s.enabled && r.enabled,
        summary: s.summary,
        turns: r.turns || [],
        insights: i.insights,
      };
    },
  });

// The generic settings schema (GET /api/settings/schema). Invalidated after a
// save so the surface reloads the server's hot-reloaded values.
export const settingsSchemaQuery = () =>
  queryOptions({
    queryKey: queryKeys.settings,
    queryFn: () => api.settingsSchema(),
  });
