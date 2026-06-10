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
  tools: ["tools"] as const,
  telemetry: ["telemetry"] as const,
  settings: ["settings", "schema"] as const,
  inbox: ["inbox"] as const,
  schedules: ["schedules"] as const,
  runtime: ["runtime"] as const,
  delegates: ["delegates"] as const,
  delegateTypes: ["delegates", "types"] as const,
  installedPlugins: ["plugins", "installed"] as const,
  pluginUpdates: ["plugins", "updates"] as const,
  fleet: ["fleet"] as const,
  archetypes: ["archetypes"] as const,
};

// The fleet of workspace agents (ADR 0042). `running` is a live-pid probe, so poll
// while mounted — a crashed agent flips to running:false on the next read.
export const fleetQuery = () =>
  queryOptions({
    queryKey: queryKeys.fleet,
    queryFn: () => api.fleet(),
    refetchInterval: 3_000,
  });

// Archetypes for the new-agent picker (Basic + installed bundles) — config, not live.
export const archetypesQuery = () =>
  queryOptions({
    queryKey: queryKeys.archetypes,
    queryFn: () => api.archetypes(),
  });

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

export const toolsQuery = () =>
  queryOptions({
    queryKey: queryKeys.tools,
    queryFn: () => api.tools(),
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

// The inbound inbox (ADR 0003) — all pending tiers. Live: the panel invalidates
// this on the `inbox.item` push event so a new stimulus appears immediately.
export const inboxQuery = () =>
  queryOptions({
    queryKey: queryKeys.inbox,
    queryFn: () => api.inbox("later", false),
  });

// Scheduled jobs over the active SchedulerBackend. Invalidated on add/cancel.
export const schedulesQuery = () =>
  queryOptions({
    queryKey: queryKeys.schedules,
    queryFn: () => api.schedules(),
  });

// Runtime status (model, middleware, skills, MCP, plugins, setup/graph state).
// Read non-suspense at the App shell (topbar health, never blanks the shell;
// the retry doubles as the desktop sidecar boot-probe) and via useSuspenseQuery
// in the System → Runtime panel — same cache key, deduped.
export const runtimeStatusQuery = () =>
  queryOptions({
    queryKey: queryKeys.runtime,
    queryFn: () => api.runtimeStatus(),
  });

// Delegate registry (ADR 0025) — read non-suspense in the Settings → Integrations
// panel so a 404 (delegates plugin disabled) degrades gracefully instead of
// blanking Settings. Invalidated after create/update/delete.
export const delegatesQuery = () =>
  queryOptions({
    queryKey: queryKeys.delegates,
    queryFn: () => api.delegates(),
    retry: false,
  });

export const installedPluginsQuery = () =>
  queryOptions({
    queryKey: queryKeys.installedPlugins,
    queryFn: () => api.installedPlugins(),
    retry: false,
  });

// Per-plugin update status (ADR 0027) — joined to the installed/runtime rows to
// render a freshness badge. The backend TTL-caches the ls-remote probe, so the
// staleTime here is generous: a re-check on every panel mount would just hit the
// cache anyway. Degrades gracefully (retry:false) if the updates API is absent.
export const pluginUpdatesQuery = () =>
  queryOptions({
    queryKey: queryKeys.pluginUpdates,
    queryFn: () => api.pluginUpdates(),
    staleTime: 5 * 60_000,
    retry: false,
  });

export const delegateTypesQuery = () =>
  queryOptions({
    queryKey: queryKeys.delegateTypes,
    queryFn: () => api.delegateTypes(),
    retry: false,
  });
