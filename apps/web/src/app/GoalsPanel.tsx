import { Button } from "@protolabsai/ui/primitives";
import {

  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { Suspense } from "react";

import { api } from "../lib/api";
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

function ago(epochSeconds: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
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

  if (!goals.length) {
    return (
      <div className="goal-row empty">
        <strong>No goals</strong>
        <span className="goal-row-meta">
          set one in chat with <code>/goal …</code>
        </span>
      </div>
    );
  }

  return (
    <>
      {goals.map((goal) => (
        <div className="goal-row" key={goal.session_id}>
          <div className="goal-row-head">
            <strong>{goal.condition || goal.session_id}</strong>
            {goal.mode === "monitor" ? <StatusPill label="monitor" tone="muted" /> : null}
            <StatusPill label={goal.status} tone={goalTone(goal.status)} />
          </div>
          <span className="goal-row-meta">
            {goal.session_id} · {goal.verifier?.type || "llm"}
            {goal.mode === "monitor" ? (
              <> · watched{goal.last_checked ? ` · checked ${ago(goal.last_checked)}` : ""}</>
            ) : (
              <> · iter {goal.iteration ?? 0}/{goal.max_iterations ?? 0}</>
            )}
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

export function GoalsPanel() {
  return (
    <section className="panel side-panel goals-panel">
      <PanelHeader
        compact
        title="Goals"
        kicker={<>the agent's standing goals · set with <code>/goal</code> in chat</>}
      />
      <ScrollArea className="goals-list" role="region" aria-label="Goals" tabIndex={0}>
        <QueryErrorResetBoundary>
          {({ reset }) => (
            <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="goals" />}>
              <Suspense fallback={<PanelSkeleton label="Loading goals…" />}>
                <GoalsList />
              </Suspense>
            </ErrorBoundary>
          )}
        </QueryErrorResetBoundary>
      </ScrollArea>
    </section>
  );
}
