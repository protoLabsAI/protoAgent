/** Live Work adapters; legacy source keys survive until registry cleanup. */
import { useEffect, useMemo, useState } from "react";
import { useQueries, useQueryClient, type UseQueryResult } from "@tanstack/react-query";

import { api, currentSlug } from "../lib/api";
import { decisionsApi, type DecisionList, type DecisionRow } from "../lib/api/decisions";
import type { DirectiveRow } from "../lib/api/chat";
import { goalsQuery, inboxQuery, queryKeys, schedulesQuery, tasksQuery, watchesQuery } from "../lib/queries";
import { onConnectionChange, onTopic } from "../lib/events";
import type {
  ActivityEntry,
  InboxItem,
  BackgroundJobDTO,
  GoalState,
  ScheduledJob,
  Task,
  WatchState,
} from "../lib/types";
import {
  decisionMeta,
  fromBackgroundJob,
  fromDecision,
  fromDirective,
  fromGoal,
  fromSchedule,
  fromTask,
  fromWatch,
  type WorkStatus,
  type GroupableWorkItem,
} from "./tone";
import { inWorkScope } from "./workGroups";
import { directiveStore } from "../chat/useDirectives";
import { scheduleWhen, typeAndPriority, watchKind } from "./rowMeta";

/** The eight work sources, in the order the Work view lays them out (§5). `activity` is a
 *  source only — its Work section was removed in S4c (the feed lives on the utility-bar
 *  Feed pill); the key survives to carry the delivery rows into the Needs-you group. */
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
  /** Explicit owner session, when the source actually supplies one. */
  sessionId?: string;
  /** The untouched source row, for the detail view to read fields the card doesn't carry. */
  raw: unknown;
};

export type WorkSource = {
  items: WorkItem[];
  /** Selector projection, retaining original wire rows. */
  groupItems?: GroupableWorkItem[];
  status: WorkSourceStatus;
  /** Epoch ms of the last SUCCESSFUL load (survives into `stale`), else null. */
  loadedAt: number | null;
  error: unknown;
  /** Refetch this one source (the §4.5 "Try again" on a failed card). */
  retry: () => void;
};

export type WorkItems = Record<WorkSourceKind, WorkSource>;

export type UseWorkItemsParams = {
  /** The focused chat, included even before it reaches the durable session index. */
  sessionId?: string;
  /** Shared scope filtering; Needs you always includes all chats. */
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
    meta: watchKind(row),
    raw: row,
  };
}
function mapTask(row: Task): WorkItem {
  return {
    kind: "task",
    id: row.id,
    status: fromTask(row.status || "open"),
    title: row.title,
    meta: typeAndPriority(row),
    sessionId: row.session_id || undefined,
    raw: row,
  };
}
function mapSchedule(row: ScheduledJob): WorkItem {
  return {
    kind: "schedule",
    id: row.id,
    status: fromSchedule(row),
    title: row.prompt,
    meta: scheduleWhen(row.schedule),
    sessionId: row.origin_session || row.context_id || undefined,
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
/** The scheduler's one-line wake header (`src/scheduler/local.py`, ADR 0079), which it
 *  prepends to a scheduled/watch/wait/goal prompt. It is runtime framing for the model,
 *  not the job's words, so a row title drops it and leads with the job's own prompt. */
const WAKE_HEADER = /^\s*\[Autonomous wake\b[^\]]*\]\s*/;

/** A human row title for an activity turn: what it responded to, minus the internal wake
 *  header (#5094), else the response, the job id or the origin. */
export function activityTitle(entry: ActivityEntry): string {
  const stimulus = entry.stimulus?.replace(WAKE_HEADER, "").trim();
  return stimulus || entry.text?.trim() || entry.trigger || entry.origin;
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
      inboxQuery(),
      {
        queryKey: workKey("directive-inventory", sessionId ?? ""),
        queryFn: async () => {
          // The focused ledger remains available even if session discovery fails.
          const focused = sessionId ? directiveStore(sessionId).refresh() : Promise.resolve();
          let inventory;
          try {
            inventory = await api.chatSessions(200);
            // Preserve the last discovered inventory on a session-index read failure.
            if (inventory.reason) throw new Error(inventory.reason);
          } catch (error) {
            await focused;
            throw error;
          }
          await focused;
          const ids = [...new Set([...(sessionId ? [sessionId] : []), ...inventory.sessions.map((s) => s.session_id)])];
          // Bound fan-out and reuse each ledger's retained rows and failure state.
          // One failed ledger must not hide successes or skip subsequent batches.
          const remaining = ids.filter((id) => id !== sessionId);
          for (let i = 0; i < remaining.length; i += 4) {
            await Promise.allSettled(remaining.slice(i, i + 4).map((id) => directiveStore(id).refresh()));
          }
          return { sessionIds: ids };
        },
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
    UseQueryResult<{ items: InboxItem[] }>,
    UseQueryResult<{ sessionIds: string[] }>,
  ];

  const directiveSessionIds = useMemo(() => [...new Set([
    ...(sessionId ? [sessionId] : []), ...(dirRes.data?.sessionIds ?? []),
  ])], [sessionId, dirRes.data?.sessionIds]);
  const [directiveVersion, setDirectiveVersion] = useState(0);
  useEffect(() => {
    const offs = directiveSessionIds.map((id) =>
      directiveStore(id).observe(() => setDirectiveVersion((v) => v + 1)));
    return () => offs.forEach((off) => off());
  }, [directiveSessionIds]);

  const keep = useMemo(() => (item: WorkItem) =>
    inWorkScope(item, { thisChatOnly, sessionId }), [thisChatOnly, sessionId]);

  const items = useMemo(() => {
    return {
      decisions: (decRes.data?.decisions ?? []).map(mapDecision).filter(keep),
      goals: (goalRes.data?.goals ?? []).map(mapGoal).filter(keep),
      watches: (watchRes.data?.watches ?? []).map(mapWatch).filter(keep),
      tasks: (taskRes.data?.issues ?? []).map(mapTask).filter(keep),
      schedules: (schedRes.data?.jobs ?? []).map(mapSchedule).filter(keep),
      background: (bgRes.data?.jobs ?? []).map(mapBackground).filter(keep),
      activity: [],
      directives: directiveSessionIds.flatMap((id) => {
        const snapshot = directiveStore(id).getSnapshot();
        return [...snapshot.active, ...snapshot.history];
      }).map(mapDirective).filter(keep),
    };
  }, [
    decRes.data,
    goalRes.data,
    watchRes.data,
    taskRes.data,
    schedRes.data,
    bgRes.data,
    actRes.data,
    directiveSessionIds,
    directiveVersion,
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
    let wasOffline = false;
    const offs = [
      // Prefix invalidation covers every focused-chat inventory cache.
      onTopic("decision.changed", invalidate(workKey("decisions"))),
      onTopic("goal.changed", invalidate(queryKeys.goals)),
      onTopic("watch.changed", invalidate(queryKeys.watches)),
      onTopic("task.changed", invalidate(queryKeys.tasks)),
      // Successful now delivery emits activity.message, not inbox.item.
      onTopic("activity.message", invalidate(queryKeys.inbox)),
      onTopic("inbox.item", invalidate(queryKeys.inbox)),
      onTopic("background.started", onBackground),
      onTopic("background.progress", onBackground),
      onTopic("background.completed", onBackground),
      onTopic("scheduler.changed", () => {
        setSchedulerSeen(true);
        void qc.invalidateQueries({ queryKey: queryKeys.schedules });
      }),
      onTopic("directive.changed", invalidate(workKey("directive-inventory"))),
      // A decision answered, expired or withdrawn while the bus was down sends no event we'll
      // ever see, so re-read the list once on reconnect (a one-shot refetch, not a poll).
      // onConnectionChange fires once on subscribe, so refetch only on an offline → online edge.
      onConnectionChange((connected) => {
        if (connected && wasOffline) {
          for (const queryKey of [workKey("decisions"), workKey("background"), workKey("directive-inventory"),
            queryKeys.goals, queryKeys.watches, queryKeys.tasks, queryKeys.schedules, queryKeys.inbox]) {
            void qc.invalidateQueries({ queryKey });
          }
        }
        wasOffline = !connected;
      }),
    ];
    return () => {
      for (const off of offs) off();
    };
  }, [qc]);

  const directiveSource = stateOf(dirRes, items.directives);
  // Inventory discovery can succeed while every ledger refresh fails. Keep the
  // timestamp tied to successful ledger reads, including retained cache data.
  if (directiveSessionIds.length) {
    directiveSource.loadedAt = Math.max(...directiveSessionIds.map((id) =>
      directiveStore(id).getSnapshot().loadedAt ?? 0)) || null;
  }
  if (directiveSource.status === "error" && directiveSource.loadedAt !== null) {
    directiveSource.status = "stale";
  }
  if (directiveSource.status === "ready" && directiveSessionIds.some((id) =>
    ["failed", "stale"].includes(directiveStore(id).getSnapshot().state))) {
    directiveSource.status = directiveSource.loadedAt === null ? "error" : "stale";
    directiveSource.error = new Error("A directive ledger could not be refreshed");
  }

  return {
    decisions: stateOf(decRes, items.decisions),
    goals: stateOf(goalRes, items.goals),
    watches: stateOf(watchRes, items.watches),
    tasks: stateOf(taskRes, items.tasks),
    schedules: {
      ...stateOf(schedRes, items.schedules),
      groupItems: (schedRes.data?.jobs ?? []).map((row): GroupableWorkItem => row.id.startsWith("wait:")
        ? { ...mapSchedule(row), kind: "wait", raw: row }
        : mapSchedule(row)).filter((item) => inWorkScope(item, { thisChatOnly, sessionId })),
    },
    background: stateOf(bgRes, items.background),
    activity: {
      ...stateOf(actRes, []),
      groupItems: (actRes.data?.items ?? []).map((row) => ({
        kind: "delivery", id: String(row.id), title: row.text, meta: row.source || "",
        status: "needs-you", raw: row,
      })),
    },
    directives: directiveSource,
  };
}
