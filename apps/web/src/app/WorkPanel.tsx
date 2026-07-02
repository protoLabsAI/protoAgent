import {
  useEffect,
  useState,
  type ComponentProps,
  type MouseEvent,
  type ReactNode,
} from "react";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Badge, Button, Empty, type Status } from "@protolabsai/ui/primitives";
import { StatusDot } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { ArrowLeft, Boxes, CalendarClock, Eye, Plus, Target } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { StagePanel } from "./ErrorBoundary";
import { GoalCreateDialog, GoalsPanel } from "./GoalsPanel";
import { WatchesPanel } from "./WatchesPanel";
import { TaskCreateDialog, TasksPanel } from "./TasksPanel";
import { ScheduleModal, SchedulePanel } from "../schedule/SchedulePanel";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { onServerEvent } from "../lib/events";
import { tasksQuery, goalsQuery, schedulesQuery, watchesQuery, queryKeys } from "../lib/queries";
import type { GoalState, ScheduledJob, Task, WatchState } from "../lib/types";
import type { IssueDraft } from "./tasks";
import {
  activeGoals,
  activeWatches,
  goalsPulse,
  schedulePulse,
  taskBuckets,
  tasksPulse,
  untilLabel,
  upcomingJobs,
  visibleWatches,
  watchesPulse,
} from "./workOverview";

import "./work.css";

type WorkView = "overview" | "goals" | "watches" | "tasks" | "schedule";
type Confirm = ComponentProps<typeof TasksPanel>["confirm"];

/**
 * The agent's WORK hub (2026-06) — one right-rail surface consolidating the "what's the
 * agent doing" panels: what it's steering toward (Goals), what it's passively supervising
 * (Watches), the concrete backlog (Tasks), and timed runs (Schedule).
 *
 * Card-first navigation (2026-07, no tabs): the landing is ALWAYS the Overview — four live
 * cards that each click through to the full panel, rendered verbatim under a slim
 * "← Overview" back bar. The view is deliberately not persisted: reopening Work lands on
 * the roll-up, drilling in is a gesture away.
 */
export function WorkPanel({ confirm }: { confirm: Confirm }) {
  const [view, setView] = useState<WorkView>("overview");
  const queryClient = useQueryClient();

  // Surface-level live roll-up: the panels' own bus subscriptions only exist while that
  // panel is MOUNTED, so with card navigation the overview (and every other card) would go
  // stale while you're inside one panel. Subscribe once at the surface level — the same
  // push pattern as the panels (invalidate the matching query key on the relevant ADR 0039
  // topic, no polling): `goal.changed`/`goal.iteration` (set/advance/clear/terminal),
  // `watch.changed`/`watch.met`/`watch.expired`/`watch.stalled` (create/check/terminal),
  // `task.changed` (filed/closed/updated), `scheduler.fired` (a job dispatched, so its
  // next_fire moved). The panels keep their own subscriptions (double-invalidate is a no-op).
  useEffect(() => {
    const refresh = (key: readonly unknown[]) => () =>
      void queryClient.invalidateQueries({ queryKey: key });
    const offs = [
      onServerEvent("goal.changed", refresh(queryKeys.goals)),
      onServerEvent("goal.iteration", refresh(queryKeys.goals)),
      onServerEvent("watch.changed", refresh(queryKeys.watches)),
      onServerEvent("watch.met", refresh(queryKeys.watches)),
      onServerEvent("watch.expired", refresh(queryKeys.watches)),
      onServerEvent("watch.stalled", refresh(queryKeys.watches)),
      onServerEvent("task.changed", refresh(queryKeys.tasks)),
      onServerEvent("scheduler.fired", refresh(queryKeys.schedules)),
    ];
    return () => offs.forEach((off) => off());
  }, [queryClient]);

  if (view === "overview") {
    return (
      <StagePanel label="work" variant="side">
        <WorkOverview onOpen={setView} />
      </StagePanel>
    );
  }

  return (
    <div
      className="work-view"
      onKeyDown={(e) => {
        // Escape backs out to the Overview — but only when no overlay owns the key: DS
        // Dialogs close on a document-level Escape handler while their .pl-overlay is
        // still mounted during this same bubble, and radix menus float in a popper
        // wrapper, so their dismissal never doubles as a navigation.
        if (e.key !== "Escape") return;
        if (document.querySelector(".pl-overlay, [data-radix-popper-content-wrapper]")) return;
        setView("overview");
      }}
    >
      <div className="work-view-bar">
        <Button variant="ghost" size="sm" type="button" onClick={() => setView("overview")} data-testid="work-back">
          <ArrowLeft size={15} /> Overview
        </Button>
      </div>
      {view === "goals" ? (
        <GoalsPanel />
      ) : view === "watches" ? (
        <WatchesPanel />
      ) : view === "tasks" ? (
        <TasksPanel confirm={confirm} />
      ) : (
        <SchedulePanel />
      )}
    </div>
  );
}

// Row-dot tones. Goals on the card are all in-flight (terminal ones are filtered), so the
// dot reads "loop running"; watches carry their full status; tasks distinguish
// in-progress from ready.
const goalDot = (status: string): Status => {
  if (status === "achieved") return "success";
  if (status === "unachievable" || status === "failed") return "error";
  return "warning";
};
const watchDot = (status: string): Status => {
  if (status === "met") return "success";
  if (status === "expired") return "error";
  if (status === "stalled") return "warning";
  if (status === "active") return "info";
  return "neutral";
};
const taskDot = (status: string | undefined): Status =>
  (status ?? "").toLowerCase().replace(/[ _-]/g, "") === "inprogress" ? "warning" : "neutral";

function WorkOverview({ onOpen }: { onOpen: (v: WorkView) => void }) {
  const goals = useSuspenseQuery(goalsQuery()).data.goals;
  const watches = useSuspenseQuery(watchesQuery()).data.watches;
  const issues = useSuspenseQuery(tasksQuery()).data.issues;
  // The scheduler bus only emits `scheduler.fired` (a dispatch) — there is NO push when the
  // AGENT adds/cancels a job mid-turn (schedule_task/cancel_schedule mutate the store
  // directly). A gentle per-use poll (NOT on the shared schedulesQuery() factory — that
  // would regress SchedulePanel and other `schedules`-key consumers) self-heals that one
  // change-class while the overview is up (#1537); chosen over a per-card refresh button
  // as the lighter affordance.
  const jobs = useSuspenseQuery({ ...schedulesQuery(), refetchInterval: 60_000 }).data.jobs;
  const queryClient = useQueryClient();
  const toast = useToast();

  // Quick-add: the overview hosts the same creator dialogs the panels use (one form, two
  // hosts) — open-state + mutation + invalidation live here, mirroring the panel hosts.
  const [goalOpen, setGoalOpen] = useState(false);
  const [taskOpen, setTaskOpen] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);

  const setGoal = useMutation({
    mutationFn: (body: { session_id: string; condition: string; verifier: unknown }) =>
      api.setGoal(body),
    onSuccess: (res) => {
      setGoalOpen(false);
      toast({ tone: "success", title: "Goal set", message: res.message || "The agent has a new goal." });
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't set goal", message: errMsg(e) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.goals }),
  });
  const createTask = useMutation({
    mutationFn: (d: IssueDraft) =>
      api.createTask({
        title: d.title.trim(),
        type: d.type,
        priority: d.priority,
        description: d.description.trim() || undefined,
      }),
    onSuccess: () => {
      setTaskOpen(false);
      toast({ tone: "success", title: "Task created", message: "Added to the board." });
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't create task", message: errMsg(e) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.tasks }),
  });
  const addSchedule = useMutation({
    mutationFn: (body: { prompt: string; schedule: string; job_id?: string; timezone?: string }) =>
      api.addSchedule(body),
    onSuccess: () => {
      setScheduleOpen(false);
      toast({ tone: "success", title: "Scheduled", message: "The job was added." });
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't schedule", message: errMsg(e) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.schedules }),
  });

  const active = activeGoals(goals);
  const watchList = visibleWatches(watches);
  const { ready, inProgress } = taskBuckets(issues);
  const upcoming = upcomingJobs(jobs);

  return (
    <>
      <div className="work-overview stage-body">
        <OverviewCard
          id="goals"
          title="Goals"
          icon={Target}
          count={active.length}
          pulse={goalsPulse(goals)}
          onOpen={() => onOpen("goals")}
          quickAdd={{ label: "Goal", testId: "work-add-goal", onAdd: () => setGoalOpen(true) }}
          empty={
            active.length === 0
              ? { title: "No active goals", description: <>set one here, or in chat with <code>/goal …</code></> }
              : null
          }
        >
          {active.slice(0, 4).map((g: GoalState) => (
            <li className="work-row" key={g.session_id}>
              <StatusDot status={goalDot(g.status)} />
              <span className="work-row-title">{g.condition}</span>
              <span className="work-row-meta">
                {g.mode === "monitor" ? "monitor" : `${g.iteration ?? 0}/${g.max_iterations ?? "∞"}`}
              </span>
            </li>
          ))}
        </OverviewCard>

        <OverviewCard
          id="watches"
          title="Watches"
          icon={Eye}
          count={activeWatches(watches).length}
          pulse={watchesPulse(watches)}
          onOpen={() => onOpen("watches")}
          // No quick-add: watches are agent-created (ADR 0067), not an operator form.
          quickAdd={null}
          empty={
            watchList.length === 0
              ? {
                  title: "No watches",
                  description: "The agent sets watches when you ask it to keep an eye on something.",
                }
              : null
          }
        >
          {watchList.slice(0, 4).map((w: WatchState) => (
            <li className="work-row" key={w.id}>
              <StatusDot status={watchDot(w.status)} />
              <span className="work-row-title">{w.condition || w.id}</span>
              <span className="work-row-meta">
                <Badge status={watchDot(w.status)}>{w.status}</Badge>
              </span>
            </li>
          ))}
        </OverviewCard>

        <OverviewCard
          id="tasks"
          title="Tasks"
          icon={Boxes}
          count={ready.length + inProgress.length}
          pulse={tasksPulse(issues)}
          onOpen={() => onOpen("tasks")}
          quickAdd={{ label: "Task", testId: "work-add-task", onAdd: () => setTaskOpen(true) }}
          empty={
            ready.length + inProgress.length === 0
              ? { title: "No open tasks", description: "add one, or the agent will file its own" }
              : null
          }
        >
          {[...inProgress, ...ready].slice(0, 4).map((i: Task) => (
            <li className="work-row" key={i.id}>
              <StatusDot status={taskDot(i.status)} />
              <span className="work-row-title">{i.title}</span>
              <span className="work-row-meta">{i.id}</span>
            </li>
          ))}
        </OverviewCard>

        <OverviewCard
          id="schedule"
          title="Schedule"
          icon={CalendarClock}
          count={upcoming.length}
          pulse={schedulePulse(jobs)}
          onOpen={() => onOpen("schedule")}
          quickAdd={{ label: "Schedule", testId: "work-add-schedule", onAdd: () => setScheduleOpen(true) }}
          empty={upcoming.length === 0 ? { title: "Nothing scheduled", description: "give the agent a timed run" } : null}
        >
          {upcoming.slice(0, 3).map((j: ScheduledJob) => (
            <li className="work-row" key={j.id}>
              <StatusDot status="info" />
              <span className="work-row-title">{j.prompt}</span>
              <span className="work-row-meta">{untilLabel(j.next_fire)}</span>
            </li>
          ))}
        </OverviewCard>
      </div>

      <GoalCreateDialog
        open={goalOpen}
        onClose={() => { setGoalOpen(false); setGoal.reset(); }}
        onCreate={(body) => setGoal.mutate(body)}
        busy={setGoal.isPending}
      />
      <TaskCreateDialog
        open={taskOpen}
        onClose={() => { setTaskOpen(false); createTask.reset(); }}
        onCreate={(d) => createTask.mutate(d)}
        busy={createTask.isPending}
      />
      <ScheduleModal
        open={scheduleOpen}
        onClose={() => { setScheduleOpen(false); addSchedule.reset(); }}
        onAdd={(b) => addSchedule.mutate(b)}
        busy={addSchedule.isPending}
      />
    </>
  );
}

// One overview card: the WHOLE card is the click-through to its panel (role=button,
// Enter/Space, selection-guarded like the chat report card), with a header (icon + name +
// count Badge), the muted one-line pulse, a short StatusDot micro-list, and a corner "+"
// quick-add that stops propagation so adding never navigates. When the card is empty the
// DS Empty takes over the body, with the same quick-add as its action (when the card has
// one — Watches doesn't).
function OverviewCard({
  id,
  title,
  icon: Icon,
  count,
  pulse,
  onOpen,
  quickAdd,
  empty,
  children,
}: {
  id: string;
  title: string;
  icon: LucideIcon;
  count: number;
  pulse?: string;
  onOpen: () => void;
  quickAdd: { label: string; testId: string; onAdd: () => void } | null;
  // `title` stays a string — the DS Empty spreads div HTMLAttributes, whose own
  // `title` (the tooltip attr) intersects the slot prop down to string.
  empty: { title?: string; description?: ReactNode } | null;
  children?: ReactNode;
}) {
  const add = quickAdd
    ? (e: MouseEvent) => {
        e.stopPropagation(); // adding is not navigating
        quickAdd.onAdd();
      }
    : undefined;
  return (
    <section
      className="work-card"
      role="button"
      tabIndex={0}
      aria-label={`Open ${title}`}
      data-testid={`work-card-${id}`}
      onClick={() => {
        // Convenience click-anywhere — but selecting a row's text must not navigate.
        if (window.getSelection()?.isCollapsed !== false) onOpen();
      }}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return; // inner buttons keep their own keys
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      <header className="work-card-head">
        <Icon size={15} className="work-card-icon" aria-hidden />
        <span className="work-card-title">{title}</span>
        <Badge status={count > 0 ? "info" : "neutral"}>{count}</Badge>
      </header>
      {empty ? (
        <Empty
          className="work-card-blank"
          title={empty.title}
          description={empty.description}
          action={
            quickAdd ? (
              <Button size="sm" type="button" data-testid={quickAdd.testId} onClick={add}>
                <Plus size={14} /> {quickAdd.label}
              </Button>
            ) : undefined
          }
        />
      ) : (
        <>
          {pulse ? <p className="work-card-pulse">{pulse}</p> : null}
          <ul className="work-card-body">{children}</ul>
          {quickAdd ? (
            <div className="work-card-foot">
              <Button
                variant="ghost"
                size="sm"
                type="button"
                title={`New ${quickAdd.label.toLowerCase()}`}
                data-testid={quickAdd.testId}
                onClick={add}
              >
                <Plus size={14} /> {quickAdd.label}
              </Button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
