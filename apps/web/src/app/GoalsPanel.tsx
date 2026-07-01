import "../goals/goals.css";

import { Input, Textarea } from "@protolabsai/ui/forms";
import { Button, Empty } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";
import {

  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { Suspense, useEffect, useState, type FormEvent } from "react";

import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
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
            set one in chat with <code>/goal …</code>
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

// Compact operator "set a goal" form, above the list. Collapsed by default (a <details>
// disclosure) so it stays out of the way — the primary surface is still the goal list. POSTs
// to the operator `/api/goals` (ADR 0066), which accepts any verifier type; the verifier is a
// small JSON textarea (default `{"type":"llm"}`) parsed on submit — invalid JSON shows an
// inline error and doesn't submit. Result is surfaced via the shared DS toast, and the goals
// query is invalidated so the list picks up the new goal.
function NewGoalForm() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [sessionId, setSessionId] = useState("operator");
  const [condition, setCondition] = useState("");
  const [verifier, setVerifier] = useState('{"type":"llm"}');
  const [jsonError, setJsonError] = useState<string | null>(null);

  const set = useMutation({
    mutationFn: (body: { session_id: string; condition: string; verifier: unknown }) =>
      api.setGoal(body),
    onSuccess: (res) => {
      setCondition("");
      toast({ tone: "success", title: "Goal set", message: res.message || "The agent has a new goal." });
    },
    // A rejected verifier / disabled goal mode comes back as HTTP 400 → request() throws here.
    onError: (e) => toast({ tone: "error", title: "Couldn't set goal", message: errMsg(e) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.goals }),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const cond = condition.trim();
    if (!cond) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(verifier.trim() || "{}");
    } catch {
      setJsonError("Verifier must be valid JSON");
      return;
    }
    setJsonError(null);
    set.mutate({ session_id: sessionId.trim() || "operator", condition: cond, verifier: parsed });
  };

  return (
    <details className="goal-new">
      <summary>New goal</summary>
      <form className="goal-new-form" onSubmit={submit}>
        <label className="field">
          <span>Session</span>
          <Input value={sessionId} onChange={(e) => setSessionId(e.target.value)} placeholder="operator" />
        </label>
        <label className="field">
          <span>Condition</span>
          <Input
            value={condition}
            onChange={(e) => setCondition(e.target.value)}
            placeholder="What the agent should achieve"
            required
          />
        </label>
        <label className="field">
          <span>Verifier (JSON)</span>
          <Textarea
            value={verifier}
            rows={2}
            spellCheck={false}
            aria-invalid={jsonError ? true : undefined}
            onChange={(e) => {
              setVerifier(e.target.value);
              if (jsonError) setJsonError(null);
            }}
          />
        </label>
        {jsonError ? (
          <p className="goal-new-error field-warn" role="alert">{jsonError}</p>
        ) : null}
        <Button
          type="submit"
          variant="primary"
          loading={set.isPending}
          disabled={!condition.trim() || set.isPending}
        >
          Set goal
        </Button>
      </form>
    </details>
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
      <NewGoalForm />
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
    </section>
  );
}
