import { useEffect, useState, type ComponentProps, type ReactNode } from "react";
import { useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Tabs } from "@protolabsai/ui/navigation";
import { Boxes, CalendarClock, ChevronRight, Eye, LayoutDashboard, Target } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { StagePanel } from "./ErrorBoundary";
import { GoalsPanel } from "./GoalsPanel";
import { WatchesPanel } from "./WatchesPanel";
import { TasksPanel } from "./TasksPanel";
import { SchedulePanel } from "../schedule/SchedulePanel";
import { onServerEvent } from "../lib/events";
import { tasksQuery, goalsQuery, schedulesQuery, queryKeys } from "../lib/queries";
import type { Task, GoalState, ScheduledJob } from "../lib/types";

import "./work.css";

type WorkTab = "overview" | "goals" | "watches" | "tasks" | "schedule";
type Confirm = ComponentProps<typeof TasksPanel>["confirm"];

const TABS: { id: WorkTab; label: string; icon: LucideIcon }[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "goals", label: "Goals", icon: Target },
  { id: "watches", label: "Watches", icon: Eye },
  { id: "tasks", label: "Tasks", icon: Boxes },
  { id: "schedule", label: "Schedule", icon: CalendarClock },
];

/**
 * The agent's WORK hub (2026-06) — one right-rail surface consolidating the three "what's
 * the agent doing" panels: what it's steering toward (Goals), the concrete backlog
 * (Tasks/Tasks), and timed runs (Schedule), with a glanceable Overview roll-up on top. The
 * Goals/Tasks/Schedule tabs reuse the standalone panels verbatim; only the Overview is new.
 */
export function WorkPanel({ confirm }: { confirm: Confirm }) {
  const [tab, setTab] = useState<WorkTab>("overview");
  return (
    <>
      <Tabs
        responsive
        active={tab}
        onSelect={(t) => setTab(t as WorkTab)}
        items={TABS.map((t) => ({ id: t.id, label: t.label, icon: <t.icon size={15} /> }))}
      />
      {tab === "overview" ? (
        <StagePanel label="work" variant="side">
          <WorkOverview onJump={setTab} />
        </StagePanel>
      ) : tab === "goals" ? (
        <GoalsPanel />
      ) : tab === "watches" ? (
        <WatchesPanel />
      ) : tab === "tasks" ? (
        <TasksPanel confirm={confirm} />
      ) : (
        <SchedulePanel />
      )}
    </>
  );
}

const normStatus = (s: string | undefined) => (s ?? "").toLowerCase().replace(/[ _-]/g, "");

function WorkOverview({ onJump }: { onJump: (t: WorkTab) => void }) {
  const goals = useSuspenseQuery(goalsQuery()).data.goals;
  const issues = useSuspenseQuery(tasksQuery()).data.issues;
  // The scheduler bus only emits `scheduler.fired` (a dispatch) — there is NO push when the
  // AGENT adds/cancels a job mid-turn (schedule_task/cancel_schedule mutate the store directly).
  // A gentle per-use poll (NOT on the shared schedulesQuery() factory — that would regress
  // SchedulePanel and other `schedules`-key consumers) self-heals that one change-class (#1537).
  const jobs = useSuspenseQuery({ ...schedulesQuery(), refetchInterval: 15_000 }).data.jobs;
  const queryClient = useQueryClient();

  // Live roll-up: the Overview is a different tab than the Goals/Tasks/Schedule panels, so
  // while it's showing those panels are unmounted and their own bus subscriptions are gone.
  // Subscribe here too so agent-driven changes mid-turn refresh the counts without a remount
  // (#1537) — the same push pattern as the panels (invalidate the matching query key on the
  // relevant ADR 0039 topic, no polling): `goal.changed`/`goal.iteration` (set/advance/clear/
  // terminal), `task.changed` (filed/closed/updated), `scheduler.fired` (a job dispatched, so
  // its next_fire moved).
  useEffect(() => {
    const refresh = (key: readonly unknown[]) => () =>
      void queryClient.invalidateQueries({ queryKey: key });
    const offs = [
      onServerEvent("goal.changed", refresh(queryKeys.goals)),
      onServerEvent("goal.iteration", refresh(queryKeys.goals)),
      onServerEvent("task.changed", refresh(queryKeys.tasks)),
      onServerEvent("scheduler.fired", refresh(queryKeys.schedules)),
    ];
    return () => offs.forEach((off) => off());
  }, [queryClient]);

  const activeGoals = goals.filter(
    (g) => g.status !== "achieved" && g.status !== "failed" && !g.finished_at,
  );
  const ready = issues.filter((i) => normStatus(i.status) === "ready");
  const inProgress = issues.filter((i) => normStatus(i.status) === "inprogress");
  const upcoming = jobs
    .filter((j) => j.enabled !== false && j.next_fire)
    .sort((a, b) => ((a.next_fire ?? "") < (b.next_fire ?? "") ? -1 : 1))
    .slice(0, 3);

  return (
    <div className="work-overview stage-body">
      <OverviewCard
        title="Goals"
        count={activeGoals.length}
        hint="active"
        onOpen={() => onJump("goals")}
        empty="No active goals — set one in chat with /goal …"
      >
        {activeGoals.slice(0, 4).map((g: GoalState) => (
          <li className="work-row" key={g.session_id}>
            <span className="work-row-title">{g.condition}</span>
            <span className="work-row-meta">
              {g.mode === "monitor" ? "monitor" : `${g.iteration ?? 0}/${g.max_iterations ?? "∞"}`}
            </span>
          </li>
        ))}
      </OverviewCard>

      <OverviewCard
        title="Tasks"
        count={ready.length + inProgress.length}
        hint={`${ready.length} ready · ${inProgress.length} in progress`}
        onOpen={() => onJump("tasks")}
        empty="No open tasks"
      >
        {[...inProgress, ...ready].slice(0, 5).map((i: Task) => (
          <li className="work-row" key={i.id}>
            <span className="work-row-title">{i.title}</span>
            <span className="work-row-meta">{i.id}</span>
          </li>
        ))}
      </OverviewCard>

      <OverviewCard
        title="Next runs"
        count={upcoming.length}
        hint="scheduled"
        onOpen={() => onJump("schedule")}
        empty="Nothing scheduled"
      >
        {upcoming.map((j: ScheduledJob) => (
          <li className="work-row" key={j.id}>
            <span className="work-row-title">{j.prompt}</span>
            <span className="work-row-meta">{whenLabel(j.next_fire)}</span>
          </li>
        ))}
      </OverviewCard>
    </div>
  );
}

function OverviewCard({
  title,
  count,
  hint,
  onOpen,
  empty,
  children,
}: {
  title: string;
  count: number;
  hint?: string;
  onOpen: () => void;
  empty: string;
  children: ReactNode;
}) {
  return (
    <section className="work-card">
      <button type="button" className="work-card-head" onClick={onOpen}>
        <span className="work-card-title">{title}</span>
        <span className="work-card-count">
          {count}
          {hint ? <span className="work-card-hint"> · {hint}</span> : null}
        </span>
        <ChevronRight size={15} className="work-card-go" />
      </button>
      {count > 0 ? (
        <ul className="work-card-body">{children}</ul>
      ) : (
        <p className="work-card-empty">{empty}</p>
      )}
    </section>
  );
}

function whenLabel(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
