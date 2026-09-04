import { queryOptions, type QueryClient } from "@tanstack/react-query";

import { api, currentSlug } from "./api";
import type { FleetTelemetry, ReviewState } from "./types";

// Centralized query keys + option factories (ADR 0013). Surfaces read these via
// `useSuspenseQuery(...)`; mutations invalidate the matching key. Keep keys
// stable and hierarchical so a mutation can invalidate a whole subtree.
//
// Every key leads with the focused agent's slug (#2887). The QueryClient is shared
// per window; today an agent switch is a full page navigation (the cache dies with
// the page), but an in-place switch must never serve agent A's cached rows under
// agent B for a staleTime window. `ns` reads the slug from the URL at ACCESS time —
// the entries are getters/functions, never module-init arrays — and the slug is a
// common prefix, so subtree invalidation (`queryKeys.goals` refreshing an open goal
// drawer) keeps working within each agent's namespace.
const ns = <const T extends readonly (string | number)[]>(...key: T) =>
  [currentSlug(), ...key] as const;

export const queryKeys = {
  get goals() {
    return ns("goals");
  },
  // One goal's detail (status + plan artifact). Prefixed under `goals` so the panel's
  // `invalidateQueries(queryKeys.goals)` on the goal.* bus pushes also refreshes an open drawer.
  goalDetail: (sessionId: string) => ns("goals", "detail", sessionId),
  get watches() {
    return ns("watches");
  },
  get verifiers() {
    return ns("verifiers");
  },
  get tasks() {
    return ns("tasks", "issues");
  },
  get workflows() {
    return ns("workflows");
  },
  // Paused workflow runs (F3 Pending Gates) — a distinct top-level key so a recipe-list
  // save/delete invalidation (queryKeys.workflows) doesn't disturb this queue, and vice versa.
  get workflowRuns() {
    return ns("workflow-runs");
  },
  // One run's live record (the Studio timeline) — under `workflow-runs` so gate
  // resume invalidations refresh an open timeline too.
  workflowRun: (runId: string) => ns("workflow-runs", "record", runId),
  get workflowRunHistory() {
    return ns("workflow-run-history");
  },
  get subagents() {
    return ns("subagents");
  },
  get tools() {
    return ns("tools");
  },
  get telemetry() {
    return ns("telemetry");
  },
  // Hub-side fleet rollup — a subkey of telemetry so a telemetry invalidation refreshes it too.
  get fleetTelemetry() {
    return ns("telemetry", "fleet");
  },
  get settings() {
    return ns("settings", "schema");
  },
  get inbox() {
    return ns("inbox");
  },
  get schedules() {
    return ns("schedules");
  },
  get runtime() {
    return ns("runtime");
  },
  get nodeRuntime() {
    return ns("runtime", "node");
  },
  get pythonRuntime() {
    return ns("runtime", "python");
  },
  get delegates() {
    return ns("delegates");
  },
  get delegateTypes() {
    return ns("delegates", "types");
  },
  // The `@`-addressable roster for the chat composer. Prefixed under `delegates` on
  // purpose: the roster IS delegate-derived, so `DelegatesSection`'s existing
  // `invalidateQueries(queryKeys.delegates)` after create/update/delete reaches it by
  // prefix match. Adding a delegate used to leave the `@` menu stale until a page
  // reload, because the composer fetched this once per mount and nothing told it to
  // look again.
  get chatMentions() {
    return ns("delegates", "mentions");
  },
  // SERVER slash commands (`/api/chat/commands` — `/goal`, plugin commands, workflows,
  // subagents, user-facing skills). Its own top-level key rather than a `plugins` subkey:
  // plugins are only ONE of its sources, so it can't ride a prefix invalidation — every
  // surface that writes one of the four names this key, via `invalidateChatCommands`.
  get chatCommands() {
    return ns("chat", "commands");
  },
  get acpAgents() {
    return ns("acp", "agents");
  },
  get installedPlugins() {
    return ns("plugins", "installed");
  },
  get pluginUpdates() {
    return ns("plugins", "updates");
  },
  get fleet() {
    return ns("fleet");
  },
  get archetypes() {
    return ns("archetypes");
  },
  get playbooks() {
    return ns("playbooks");
  },
  get knowledge() {
    return ns("knowledge");
  },
  get flags() {
    return ns("flags");
  },
  get secretsStatus() {
    return ns("secrets", "status");
  },
  get publishedLinks() {
    return ns("publish", "links");
  },
  // Memory inspector (ADR 0069 D7) — subtree so one invalidate refreshes all panels.
  get memory() {
    return ns("memory");
  },
  get memorySessions() {
    return ns("memory", "sessions");
  },
  get memoryHot() {
    return ns("memory", "hot");
  },
  get memoryInjections() {
    return ns("memory", "injections");
  },
  memoryInjectionDetail: (id: number) => ns("memory", "injections", "detail", id),
};

// The fleet of workspace agents (ADR 0042). `running` is a live-pid probe, so poll
// while mounted — a crashed agent flips to running:false on the next read.
export const fleetQuery = () =>
  queryOptions({
    queryKey: queryKeys.fleet,
    queryFn: () => api.fleet(),
    refetchInterval: 3_000,
  });

// Developer flags (ADR 0068) — the active channel + resolved flag states. Static per
// process (channel + registry don't change without a config edit / restart), so no poll.
export const flagsQuery = () =>
  queryOptions({
    queryKey: queryKeys.flags,
    queryFn: () => api.flags(),
    staleTime: 5 * 60_000,
  });

// External secrets-manager status (ADR 0080) — last hydration outcome + owned var
// names. Slow poll so a background-loop refresh shows up while the panel is open;
// Sync-now invalidates immediately.
export const secretsStatusQuery = () =>
  queryOptions({
    queryKey: queryKeys.secretsStatus,
    queryFn: () => api.secretsStatus(),
    refetchInterval: 30_000,
  });

// Published chat threads (#2684) — no poll; a publish/revoke mutation invalidates
// this key directly, same as the other manager lists (devices, watches).
export const publishedLinksQuery = () =>
  queryOptions({
    queryKey: queryKeys.publishedLinks,
    queryFn: () => api.publishedLinks(),
  });

// Archetypes for the new-agent picker (Basic + installed bundles) — config, not live.
export const archetypesQuery = () =>
  queryOptions({
    queryKey: queryKeys.archetypes,
    queryFn: () => api.archetypes(),
  });

// Goals the agent works toward (goal mode). Lives in the right sidebar; the panel
// invalidates this on the `goal.changed` bus push (the agent set/advances/clears goals
// mid-turn) instead of a 5s poll (#1310) — same pattern as the inbox.
export const goalsQuery = () =>
  queryOptions({
    queryKey: queryKeys.goals,
    queryFn: () => api.goals(),
  });

// One goal's detail (status + `.plan.md`) for the detail drawer. Keyed by session so each
// goal caches independently; the `["goals"]` prefix means the panel's bus invalidation
// refreshes an open drawer live as the loop iterates.
export const goalDetailQuery = (sessionId: string) =>
  queryOptions({
    queryKey: queryKeys.goalDetail(sessionId),
    queryFn: () => api.goalDetail(sessionId),
  });

// Passive watches (ADR 0067) — verifier-only objectives polled out-of-band. Lives in the
// Work hub; the panel invalidates this on the `watch.*` bus pushes (created/checked/met/
// expired/stalled) instead of a poll — same pattern as goals.
// The verifier catalog (ADR 0028/0067) changes only when plugins load/reload, so it's
// effectively static per session — but it must NOT be suspense-loaded: a failed fetch has to
// degrade to the core types rather than block the creator dialog behind an error boundary.
export const verifiersQuery = () => ({
  queryKey: queryKeys.verifiers,
  queryFn: () => api.verifiers(),
  staleTime: 5 * 60_000,
});

export const watchesQuery = () =>
  queryOptions({
    queryKey: queryKeys.watches,
    queryFn: () => api.watches(),
  });

// The agent's task board (in-process tasks store — always available). The panel
// invalidates this on the `task.changed` bus push (issues the agent files/closes
// mid-turn) instead of a 5s poll (#1310).
export const tasksQuery = () =>
  queryOptions({
    queryKey: queryKeys.tasks,
    queryFn: () => api.tasks(),
  });

// Registered workflow recipes + the subagent registry — config, not live, so no
// poll; invalidated when the agent/console saves or deletes one.
export const workflowsQuery = () =>
  queryOptions({
    queryKey: queryKeys.workflows,
    queryFn: () => api.workflows(),
  });

// Paused workflow runs (F3 Pending Gates). Polled while the panel is mounted so a run the
// agent parks mid-turn shows up, and invalidated after each approve/edit/reject action.
export const workflowRunsQuery = () =>
  queryOptions({
    queryKey: queryKeys.workflowRuns,
    queryFn: () => api.workflowRuns(),
    refetchInterval: 5_000,
  });

// One run's live record — the Studio's timeline. Polls fast while the run is
// running/paused (steps flip in place) and stops on a terminal status.
export const workflowRunQuery = (runId: string) =>
  queryOptions({
    queryKey: queryKeys.workflowRun(runId),
    queryFn: () => api.workflowRun(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "done" || status === "failed" ? false : 1_200;
    },
  });

// Run history summaries — refreshed when the watched run reaches a terminal state
// (the timeline invalidates it), plus a slow poll for runs started elsewhere (chat).
export const workflowRunHistoryQuery = () =>
  queryOptions({
    queryKey: queryKeys.workflowRunHistory,
    queryFn: () => api.workflowRunHistory(),
    refetchInterval: 15_000,
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
        traceUrlTemplate: r.langfuse_trace_url_template ?? null,
        // #3017: an older backend omits the flag. Treat that as "tracing on" so the
        // surface keeps its pre-#3017 behavior (a blank cell) rather than claiming
        // tracing is off on a backend that never told us either way.
        tracingEnabled: r.tracing_enabled ?? true,
        insights: i.insights,
      };
    },
  });

// The hub-side fleet rollup (ADR 0006 fleet extension) feeding the Telemetry
// surface's Fleet section. Read defensively: a backend without the fleet route (an
// older single-box install) degrades to `fleet: false` so the section stays hidden
// and never fails the per-instance view.
export const fleetTelemetryQuery = () =>
  queryOptions({
    queryKey: queryKeys.fleetTelemetry,
    queryFn: async () => {
      try {
        return await api.telemetryFleet();
      } catch {
        return {
          enabled: false,
          summary: null,
          insights: null,
          fleet: false,
          langfuse_trace_url_template: null,
          members: {},
        } satisfies FleetTelemetry;
      }
    },
  });

// The generic settings schema (GET /api/settings/schema). Invalidated after a
// save so the surface reloads the server's hot-reloaded values.
export const settingsSchemaQuery = (host = false) =>
  queryOptions({
    // Host-forced reads are the hub's box settings even in a member window, so they
    // intentionally live outside that window's focused-agent namespace.
    queryKey: host ? (["host", "settings", "schema"] as const) : queryKeys.settings,
    queryFn: () => api.settingsSchema(host),
    // The schema GET does a gateway round-trip server-side (it embeds the live
    // model list for the model pickers) and is read by the Settings surface AND
    // every chat tab's composer picker — so without a staleTime React Query would
    // refire it on every mount/focus. A save still invalidates it (freshness on
    // change); between saves it's served from cache.
    staleTime: 5 * 60_000,
  });

// The inbound inbox (ADR 0003) — all pending tiers. Live: the panel invalidates
// this on the `inbox.item` push event so a new stimulus appears immediately.
export const inboxQuery = () =>
  queryOptions({
    queryKey: queryKeys.inbox,
    queryFn: () => api.inbox("later", false),
  });

// A pending priority-`now` inbox item is a BLOCKED page, not an ordinary queued stimulus
// (ADR 0003, #3351). A now item only reaches the pull queue after its automatic self-A2A
// delivery failed / was refused / was storm-blocked: a fire the A2A turn ACCEPTS is marked
// delivered BEFORE it ever returns from the inbox list and pushes `activity.message` (not
// `inbox.item`), so a fired now never appears here. A now item still showing as pending is
// therefore an operator-facing fallback + diagnostic signal to triage — surfaced
// conspicuously and distinctly from next/later, but never silently marked delivered.
export function isPendingNowInboxPage(item: { priority?: unknown; delivered_at?: unknown }): boolean {
  return item.priority === "now" && !item.delivered_at;
}

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

// Managed Node runtime (ADR 0085) — status + install progress. Polls once a second
// only while an install is in flight (the download takes seconds); idle otherwise.
export const nodeRuntimeQuery = () =>
  queryOptions({
    queryKey: queryKeys.nodeRuntime,
    queryFn: () => api.nodeRuntime(),
    refetchInterval: (query) => (query.state.data?.install.state === "running" ? 1_000 : false),
  });

// Managed Python runtime (ADR 0094) — status + install progress. Same polling contract
// as the Node card: once a second only while an install is in flight (the download +
// pip-baseline phases take tens of seconds); idle otherwise.
export const pythonRuntimeQuery = () =>
  queryOptions({
    queryKey: queryKeys.pythonRuntime,
    queryFn: () => api.pythonRuntime(),
    refetchInterval: (query) => (query.state.data?.install.state === "running" ? 1_000 : false),
  });

// The HUB's runtime status (never slug-routed) — used ONLY for the tenant uid, which
// must track the origin's backend, not the focused agent. Deliberately OUTSIDE the
// slug namespace (#2887) and separate from the slug-prefixed `runtime` key, so
// switching agents never confuses it; the uid is stable, so it doesn't poll.
export const hostRuntimeStatusQuery = () =>
  queryOptions({
    queryKey: ["runtime", "host"] as const,
    queryFn: () => api.hostRuntimeStatus(),
    staleTime: Infinity,
  });

// Delegate registry (ADR 0025) — read non-suspense in the Settings → Capabilities
// panel so a 404 (delegates plugin disabled) degrades gracefully instead of
// blanking Settings. Invalidated after create/update/delete.
export const delegatesQuery = () =>
  queryOptions({
    queryKey: queryKeys.delegates,
    queryFn: () => api.delegates(),
    retry: false,
  });

// `@`-addressable participants for the composer (#3042). Served by the same resolver
// the chat dispatcher routes with, so the popover can't offer an unreachable name.
// `retry: false` mirrors delegatesQuery — a 404 (delegates plugin disabled) is an
// empty roster, not an error worth retrying.
export const chatMentionsQuery = () =>
  queryOptions({
    queryKey: queryKeys.chatMentions,
    queryFn: () => api.chatMentions(),
    retry: false,
  });

// SERVER slash commands for the composer's `/` menu (and anything else that lists them —
// the ⌘K palette next). Was a bare `useEffect` + `api.chatCommands()` + slot-local
// useState inside ChatSessionSlot, so EVERY open chat tab refetched the same list on
// mount with no shared cache and no key anything could invalidate. One query key, one
// fetch.
//
// NO staleTime override, deliberately. The endpoint is LIVE, not static:
// `_operator_chat_commands` re-resolves the registries per request, so the answer changes
// with no restart. The client default (5s, queryClient.ts) is what keeps this a strict
// improvement on the per-slot fetch it replaces rather than a regression: the slots that
// mount together at boot still share ONE fetch (the dedupe this key exists for), while a
// chat tab opened later still refetches on mount exactly as the old `useEffect` did. A
// longer window would buy nothing and cost freshness — `staleTime` schedules no refetch,
// it only withholds one, and with `refetchOnWindowFocus: false` and no `refetchInterval`
// the ONLY triggers are a new observer mounting while stale and an explicit invalidation.
//
// Invalidation is the other half, because the list folds in four sources
// (graph/slash_commands.py): plugin commands, workflows, subagents and user-facing skills.
// Each console surface that writes one must call `invalidateChatCommands` — see its note.
//
// No `retry: false` here — unlike chatMentions a 404 isn't an expected answer, and the
// shared default (one retry, riding out cold-start codes) is a strict improvement on the
// fire-and-forget `.catch(() => {})` this replaces. It's a plain `useQuery`, so a failure
// just leaves the server rows out of the menu (client commands still list) rather than
// tripping an error boundary.
export const chatCommandsQuery = () =>
  queryOptions({
    queryKey: queryKeys.chatCommands,
    queryFn: () => api.chatCommands(),
  });

/** Refresh the `/`-menu command list after a mutation that changes what
 *  `/api/chat/commands` answers.
 *
 *  Call this from EVERY such path. The endpoint folds FOUR registries into one list
 *  (graph/slash_commands.py `resolve_slash_commands`) — plugin `register_chat_command`
 *  tokens, workflow recipes, subagents, and user-facing skills — so no single surface owns
 *  the key and no key prefix covers it: the plugins manager, Workflow Studio and the
 *  Playbooks editor each invalidate a DIFFERENT key for their own list, and every one of
 *  them also changes this one. Missing the call doesn't break a surface, it just leaves the
 *  newly-created `/command` out of the composer's autocomplete until the next chat tab
 *  opens (5s staleness) or a reload — the quiet failure `chatCommandsFreshness.test.ts` guards.
 *
 *  Not everything HAS a console mutation: a skill the agent authors mid-turn
 *  (graph/skills/authoring) publishes no console event and no bus topic, so it surfaces on
 *  the next newly-mounted chat tab rather than instantly. */
export function invalidateChatCommands(qc: QueryClient): Promise<void> {
  return qc.invalidateQueries({ queryKey: queryKeys.chatCommands });
}

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

export const acpAgentsQuery = () =>
  queryOptions({
    queryKey: queryKeys.acpAgents,
    queryFn: () => api.acpAgents(),
    staleTime: Infinity, // a static catalog — fetch once
    retry: false,
  });

// The skills/playbooks index (ADR 0009) — the list view (no prompt bodies). The
// surface filters it client-side; invalidated after author/edit/promote/unshare
// (a delete updates the cache directly since it removes a single known row).
export const playbooksQuery = () =>
  queryOptions({
    queryKey: queryKeys.playbooks,
    queryFn: () => api.playbooks(),
  });

// Knowledge-store search (ADR 0020) — server-side FTS keyed on the (debounced)
// query string, so each term is its own cache entry. The surface reads it
// non-suspense with `placeholderData: keepPreviousData` so typing doesn't blank
// the list; invalidated (whole `knowledge` subtree) after curate / share / ingest.
// `reviewState` (ADR 0108 D7) keys the "pending review" queue as its own cache entry
// so toggling the filter never blanks or mislabels the unfiltered list.
export const knowledgeQuery = (q: string, reviewState?: ReviewState) =>
  queryOptions({
    // The unfiltered key keeps its historical shape ([slug, "knowledge", q]); the
    // review filter is an extra trailing segment only when it is set.
    queryKey: reviewState
      ? ([...queryKeys.knowledge, q, `review:${reviewState}`] as const)
      : ([...queryKeys.knowledge, q] as const),
    queryFn: () => api.knowledgeSearch(q, { reviewState }),
  });
