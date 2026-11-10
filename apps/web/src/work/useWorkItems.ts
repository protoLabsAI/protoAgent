/**
 * useWorkItems — the one data hook behind the Work view (#4955 §7.2, §5).
 *
 * It folds the agent's EIGHT work sources (decisions, goals, watches, tasks, schedules,
 * background jobs, activity and directives) into one uniform shape: each source reports its
 * own `{ items, status, loadedAt, error, retry }`, so the Work view can render a section
 * per source and show one failing while the rest stay live. The raw rows are normalized to
 * a flat `WorkItem` whose status runs through the bd-0ia51.4 mappers in `./tone`, so every
 * source speaks one `WorkStatus` vocabulary.
 *
 * Freshness is bus-driven, not polled: a single `onTopic` subscription invalidates exactly
 * the source a `decision.changed` / `goal.changed` / `watch.changed` / `task.changed` /
 * `background.*` / `scheduler.changed` / `directive.changed` push names (bd-0ia51.8).
 * Schedules are the one exception — the backend only emits `scheduler.changed` from
 * bd-0ia51.8 onward, so they poll every 60s UNTIL the first such event proves the bus is
 * wired, then stop.
 *
 * It owns the goals/watches/tasks/schedules TanStack keys from `lib/queries`, so an
 * invalidation here and anywhere else in the console share one cache entry; the other four
 * sources get their own `[slug, "work", …]` keys.
 */
import { useEffect, useMemo, useState } from "react";
import { useQueries, useQueryClient, type UseQueryResult } from "@tanstack/react-query";

import { api, currentSlug } from "../lib/api";
import { decisionsApi, type DecisionList, type DecisionRow } from "../lib/api/decisions";
import type { DirectiveList, DirectiveRow } from "../lib/api/chat";
import { goalsQuery, queryKeys, schedulesQuery, tasksQuery, watchesQuery } from "../lib/queries";
import { onTopic } from "../lib/events";
import type {
  ActivityEntry,
  ActivityHistory,
  BackgroundJobDTO,
  GoalState,
  ScheduledJob,
  Task,
  WatchState,
} from "../lib/types";
import {
  decisionMeta,
  fromActivity,
  fromBackgroundJob,
  fromDecision,
  fromDirective,
  fromGoal,
  fromSchedule,
  fromTask,
  fromWatch,
  type WorkStatus,
} from "./tone";

/** The eight work sources, in the order the Work view lays them out (§5). */
export type WorkSourceKind =
  | "decisions"
  | "goals"
  | "watches"
  | "tasks"
  | "schedules"
  | "background"
  | "activity"
  | "directives";

/** A source's load lifecycle. `stale` = a refresh failed AFTER a good load, so the last
 *  rows are still shown; `error` = it never loaded at all. */
export type WorkSourceStatus = "loading" | "ready" | "error" | "stale";

/** A single item's kind — the singular of its source (one `decision`, one `goal`), matching
 *  the labels the `./tone` mappers already use. */
export type WorkItemKind =
  | "decision"
  | "goal"
  | "watch"
  | "task"
  | "schedule"
  | "background"
  | "activity"
  | "directive";

/** One normalized row, whatever its source — the shape every Work surface renders (§5). */
export type WorkItem = {
  kind: WorkItemKind;
  id: string;
  status: WorkStatus;
  title: string;
  meta: string;
  /** The session a `thisChatOnly` view filters on — absent for the session-less sources
   *  (watches, tasks, schedules). */
  sessionId?: string;
  /** The untouched source row, for the detail view to read fields the card doesn't carry. */
  raw: unknown;
};

export type WorkSource = {
  items: WorkItem[];
  status: WorkSourceStatus;
  /** Epoch ms of the last SUCCESSFUL load (survives into `stale`), else null. */
  loadedAt: number | null;
  error: unknown;
  /** Refetch this one source (the §4.5 "Try again" on a failed card). */
  retry: () => void;
};

export type WorkItems = Record<WorkSourceKind, WorkSource>;

export type UseWorkItemsParams = {
  /** The focused chat, for `thisChatOnly` filtering and the directive ledger (per-session). */
  sessionId?: string;
  /** Keep only items whose session matches `sessionId`, for every source that carries one. */
  thisChatOnly: boolean;
};

/** How often schedules refetch before the first `scheduler.changed` proves the bus is live. */
export const SCHEDULE_POLL_MS = 60_000;

/** A `[slug, "work", …]` key for the four sources without a factory in `lib/queries`. */
function workKey(source: string, extra?: string): readonly unknown[] {
  const base = [currentSlug(), "work", source];
  return extra === undefined ? base : [...base, extra];
}

// ── raw row → WorkItem mappers (status via ./tone) ────────────────────────────
function mapDecision(row: DecisionRow): WorkItem {
  return {
    kind: "decision",
    id: row.id,
    status: fromDecision(row.state),
    title: row.question,
    // The Overview "Needs you" pin shows the §2.4 meta — "Asked {time}" while pending — not the
    // raw agent name; the agent (field 5) rides the per-section row in the agent-wide filter.
    meta: decisionMeta(row, false),
    sessionId: row.session_id || undefined,
    raw: row,
  };
}
function mapGoal(row: GoalState): WorkItem {
  return {
    kind: "goal",
    id: row.session_id,
    status: fromGoal(row.status),
    title: row.condition,
    meta: row.outcome || row.verifier?.type || "",
    sessionId: row.session_id || undefined,
    raw: row,
  };
}
function mapWatch(row: WatchState): WorkItem {
  return {
    kind: "watch",
    id: row.id,
    status: fromWatch(row.status, row.repeat),
    title: row.condition,
    meta: row.trigger || "",
    raw: row,
  };
}
function mapTask(row: Task): WorkItem {
  return {
    kind: "task",
    id: row.id,
    status: fromTask(row.status || "open"),
    title: row.title,
    meta: row.type || row.issue_type || "",
    raw: row,
  };
}
function mapSchedule(row: ScheduledJob): WorkItem {
  return {
    kind: "schedule",
    id: row.id,
    status: fromSchedule(row),
    title: row.prompt,
    meta: row.schedule,
    raw: row,
  };
}
function mapBackground(row: BackgroundJobDTO): WorkItem {
  return {
    kind: "background",
    id: row.id,
    status: fromBackgroundJob(row.status),
    title: row.description,
    meta: row.subagent_type,
    sessionId: row.origin_session || undefined,
    raw: row,
  };
}
function mapActivity(entry: ActivityEntry, contextId: string): WorkItem {
  return {
    kind: "activity",
    id: String(entry.id),
    status: fromActivity(entry.state),
    title: entry.stimulus?.trim() || entry.text || entry.trigger || entry.origin,
    meta: entry.origin,
    sessionId: contextId || undefined,
    raw: entry,
  };
}
function mapDirective(row: DirectiveRow): WorkItem {
  return {
    kind: "directive",
    id: row.id,
    status: fromDirective(row.status),
    title: row.quote,
    meta: row.scope || "",
    sessionId: row.session_id || undefined,
    raw: row,
  };
}

/** Normalize one query result into a `WorkSource`. Success → ready; an error WITH retained
 *  data (a failed refetch) → stale; an error with none → error; an idle, never-fetched query
 *  (directives before a session is known) → ready/empty; anything still in flight → loading. */
function stateOf<T>(result: UseQueryResult<T>, items: WorkItem[]): WorkSource {
  const status: WorkSourceStatus = result.isSuccess
    ? "ready"
    : result.isError
      ? result.data !== undefined
        ? "stale"
        : "error"
      : result.fetchStatus === "idle"
        ? "ready"
        : "loading";
  return {
    items,
    status,
    loadedAt: result.dataUpdatedAt || null,
    error: result.isError ? result.error : null,
    retry: () => {
      void result.refetch();
    },
  };
}

export function useWorkItems({ sessionId, thisChatOnly }: UseWorkItemsParams): WorkItems {
  const qc = useQueryClient();
  // Schedules poll until the first `scheduler.changed` proves the bus carries it (§7.2).
  const [schedulerSeen, setSchedulerSeen] = useState(false);

  const results = useQueries({
    queries: [
      { queryKey: workKey("decisions"), queryFn: () => decisionsApi.listDecisions() },
      goalsQuery(),
      watchesQuery(),
      tasksQuery(),
      { ...schedulesQuery(), refetchInterval: schedulerSeen ? (false as const) : SCHEDULE_POLL_MS },
      { queryKey: workKey("background"), queryFn: () => api.background() },
      { queryKey: workKey("activity"), queryFn: () => api.activity() },
      {
        queryKey: workKey("directives", sessionId ?? ""),
        queryFn: () => api.listDirectives(sessionId as string),
        enabled: !!sessionId,
      },
    ],
  });
  const [decRes, goalRes, watchRes, taskRes, schedRes, bgRes, actRes, dirRes] = results as [
    UseQueryResult<DecisionList>,
    UseQueryResult<{ goals: GoalState[]; enabled: boolean }>,
    UseQueryResult<{ watches: WatchState[]; enabled: boolean }>,
    UseQueryResult<{ issues: Task[] }>,
    UseQueryResult<{ jobs: ScheduledJob[]; backend: string }>,
    UseQueryResult<{ enabled: boolean; jobs: BackgroundJobDTO[] }>,
    UseQueryResult<ActivityHistory>,
    UseQueryResult<DirectiveList>,
  ];

  // Keep only this chat's items — but only for sources that actually carry a session id;
  // a session-less item (a watch, task or schedule) is never filtered out.
  const keep = useMemo(() => {
    const sid = thisChatOnly ? sessionId : undefined;
    return (item: WorkItem) => !sid || item.sessionId == null || item.sessionId === sid;
  }, [thisChatOnly, sessionId]);

  const items = useMemo(() => {
    const act = actRes.data;
    return {
      decisions: (decRes.data?.decisions ?? []).map(mapDecision).filter(keep),
      goals: (goalRes.data?.goals ?? []).map(mapGoal).filter(keep),
      watches: (watchRes.data?.watches ?? []).map(mapWatch).filter(keep),
      tasks: (taskRes.data?.issues ?? []).map(mapTask).filter(keep),
      schedules: (schedRes.data?.jobs ?? []).map(mapSchedule).filter(keep),
      background: (bgRes.data?.jobs ?? []).map(mapBackground).filter(keep),
      activity: (act?.entries ?? []).map((e) => mapActivity(e, act!.context_id)).filter(keep),
      directives: (dirRes.data?.directives ?? []).map(mapDirective).filter(keep),
    };
  }, [
    decRes.data,
    goalRes.data,
    watchRes.data,
    taskRes.data,
    schedRes.data,
    bgRes.data,
    actRes.data,
    dirRes.data,
    keep,
  ]);

  // One subscription for the whole hook: each push invalidates ONLY the source it names, so a
  // `watch.changed` refetches watches and nothing else (§7.2). Re-subscribes only if the
  // QueryClient identity changes (it doesn't — an agent switch is a full navigation).
  useEffect(() => {
    const invalidate = (queryKey: readonly unknown[]) => () => {
      void qc.invalidateQueries({ queryKey });
    };
    const onBackground = invalidate(workKey("background"));
    const offs = [
      // `invalidate(workKey("decisions"|"directives"))` is a PREFIX match, so the directives
      // key matches whatever session segment it carries — the effect stays `[qc]`-only and
      // never re-subscribes on a session switch.
      onTopic("decision.changed", invalidate(workKey("decisions"))),
      onTopic("goal.changed", invalidate(queryKeys.goals)),
      onTopic("watch.changed", invalidate(queryKeys.watches)),
      onTopic("task.changed", invalidate(queryKeys.tasks)),
      onTopic("background.started", onBackground),
      onTopic("background.progress", onBackground),
      onTopic("background.completed", onBackground),
      onTopic("scheduler.changed", () => {
        setSchedulerSeen(true);
        void qc.invalidateQueries({ queryKey: queryKeys.schedules });
      }),
      onTopic("directive.changed", invalidate(workKey("directives"))),
    ];
    return () => {
      for (const off of offs) off();
    };
  }, [qc]);

  return {
    decisions: stateOf(decRes, items.decisions),
    goals: stateOf(goalRes, items.goals),
    watches: stateOf(watchRes, items.watches),
    tasks: stateOf(taskRes, items.tasks),
    schedules: stateOf(schedRes, items.schedules),
    background: stateOf(bgRes, items.background),
    activity: stateOf(actRes, items.activity),
    directives: stateOf(dirRes, items.directives),
  };
}
