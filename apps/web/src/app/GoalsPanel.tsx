import "../goals/goals.css";

import { Button, Empty } from "@protolabsai/ui/primitives";
import { Dialog, useToast } from "@protolabsai/ui/overlays";
import {
  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { Suspense, useEffect, useState } from "react";

import { api } from "../lib/api";
import { HitlForm } from "../chat/HitlForm";
import { buildGoalSetBody, goalFormPayload, type GoalSetBody } from "../chat/goalForm";
import { errMsg } from "../lib/format";
import { onServerEvent } from "../lib/events";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { goalsQuery, queryKeys } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { ScrollArea } from "@protolabsai/ui/data";
import { StatusPill } from "./StatusPill";

// The agent's goals (autonomy layer), in the right sidebar. First surface on
// the TanStack Query + Suspense + ErrorBoundary data layer (ADR 0013): the read
// is a `useSuspenseQuery` (loading → <Suspense>, failure → <ErrorBoundary>),
// and clearing a goal is a `useMutation` that invalidates the goals query. No
// useEffect / busy flag / try-catch / manual refresh.

function goalTone(status: string) {
  if (status === "achieved") return "success" as const;
  if (status === "active") return "warning" as const;
  if (status === "unachievable") return "error" as const;
  return "muted" as const;
}

const trunc = (t: string, n = 80) => (t.length > n ? `${t.slice(0, n)}…` : t);

function GoalsList() {
  const { data } = useSuspenseQuery(goalsQuery());
  const goals = data.goals;
  const queryClient = useQueryClient();
  const clear = useMutation({
    mutationFn: (sessionId: string) => api.clearGoal(sessionId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.goals }),
  });

  // Live: refresh off the goal bus instead of polling every 5s (#1310), the same pattern as
  // the inbox panel. `goal.changed` fires when the agent set/advanced/cleared a goal mid-turn;
  // `goal.iteration` fires on each drive-goal continuation (ADR 0051 Slice 3) so the row's
  // `iter N/max` + last-reason updates live while the loop runs.
  useEffect(() => {
    const refresh = () => void queryClient.invalidateQueries({ queryKey: queryKeys.goals });
    const offs = [onServerEvent("goal.changed", refresh), onServerEvent("goal.iteration", refresh)];
    return () => offs.forEach((off) => off());
  }, [queryClient]);

  if (!goals.length) {
    return (
      <Empty
        title="No goals"
        description={
          <>
            set one with <code>New goal</code>, or in chat with <code>/goal …</code>
          </>
        }
      />
    );
  }

  return (
    <>
      {goals.map((goal) => (
        <div className="goal-row" key={goal.session_id}>
          <div className="goal-row-head">
            <strong>{goal.condition || goal.session_id}</strong>
            <StatusPill label={goal.status} tone={goalTone(goal.status)} />
          </div>
          <span className="goal-row-meta">
            {goal.session_id} · {goal.verifier?.type || "llm"} · iter {goal.iteration ?? 0}/
            {goal.max_iterations ?? 0}
            {goal.last_reason ? ` · ${trunc(goal.last_reason)}` : ""}
          </span>
          <Button
            icon variant="ghost" className="goal-row-clear"
            type="button"
            onClick={() => clear.mutate(goal.session_id)}
            disabled={clear.isPending}
            title="Clear goal"
          >
            <Trash2 size={15} />
          </Button>
        </div>
      ))}
    </>
  );
}

// Operator "set a goal" dialog — ONE creator, two hosts (mirroring TaskCreateDialog /
// ScheduleModal): the Goals panel's "New goal" header action and the Work overview's
// Goals-card quick-add both open it. It hosts the guided two-step wizard (ADR 0073) — the
// SAME `goalFormPayload` + `HitlForm` the chat `/goal new` composer form renders, so the
// verifier cards and the optional completion contract are identical everywhere. The host
// owns open-state + the setGoal mutation; this component is fields + mapping only.
//
// The DS Dialog is chromeless (no title/footer): `HitlForm` brings its own title, stepper,
// and Dismiss/Back/Next/Submit actions, so a Dialog header would just duplicate them. The
// answers map through the shared `buildGoalSetBody` to the `{session_id, condition,
// verifier, ...contract}` body POSTed to the operator `/api/goals` (ADR 0066/0073), which
// accepts any verifier type. Goals set here target the `operator` session.
export function GoalCreateDialog({
  open,
  onClose,
  onCreate,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  onCreate: (body: GoalSetBody) => void;
  busy: boolean;
}) {
  return (
    <Dialog open={open} onClose={onClose} width="min(560px, 94vw)" className="goal-create-modal">
      <div data-testid="goal-create-dialog">
        <HitlForm
          payload={goalFormPayload()}
          busy={busy}
          onSubmit={(answers) => {
            const body = buildGoalSetBody(
              "operator",
              typeof answers === "object" && answers ? (answers as Record<string, unknown>) : {},
            );
            if (body) onCreate(body);
          }}
          onCancel={onClose}
        />
      </div>
    </Dialog>
  );
}

export function GoalsPanel() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const set = useMutation({
    mutationFn: (body: GoalSetBody) => api.setGoal(body),
    onSuccess: (res) => {
      setCreating(false);
      toast({ tone: "success", title: "Goal set", message: res.message || "The agent has a new goal." });
    },
    // A rejected verifier / disabled goal mode comes back as HTTP 400 → request() throws here.
    onError: (e) => toast({ tone: "error", title: "Couldn't set goal", message: errMsg(e) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.goals }),
  });
  return (
    <section className="panel side-panel goals-panel">
      <PanelHeader
        compact
        title="Goals"
        kicker={<>the agent's standing goals · set with <code>/goal</code> in chat</>}
        actions={
          <Button variant="primary" type="button" onClick={() => { set.reset(); setCreating(true); }} data-testid="goal-new">
            <Plus size={16} /> New goal
          </Button>
        }
      />
      <ScrollArea className="goals-list" role="region" aria-label="Goals" tabIndex={0}>
        <QueryErrorResetBoundary>
          {({ reset }: { reset: () => void }) => (
            <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="goals" />}>
              <Suspense fallback={<PanelSkeleton label="Loading goals…" />}>
                <GoalsList />
              </Suspense>
            </ErrorBoundary>
          )}
        </QueryErrorResetBoundary>
      </ScrollArea>
      <GoalCreateDialog
        open={creating}
        onClose={() => { setCreating(false); set.reset(); }}
        onCreate={(body) => set.mutate(body)}
        busy={set.isPending}
      />
    </section>
  );
}
