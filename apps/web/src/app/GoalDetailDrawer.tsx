import { Button } from "@protolabsai/ui/primitives";
import { Drawer, useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RotateCcw, Target } from "lucide-react";

import { api } from "../lib/api";
import { Markdown } from "../chat/LazyMarkdown";
import { verifierLabel } from "../chat/goalForm";
import { ago, errMsg } from "../lib/format";
import { goalDetailQuery, queryKeys } from "../lib/queries";
import type { GoalState } from "../lib/types";
import { goalTone } from "./GoalsPanel";
import { StatusPill } from "./StatusPill";

// How many iterations the "Add iterations" action grants at once.
const EXTEND_BY = 4;

// Read-only goal detail — a right drawer opened by clicking a Goals-panel row. It surfaces
// what the console couldn't see before (ADR 0073/0079): the completion contract read-back,
// the loop's progress + last verifier reason/evidence, and the durable PLAN artifact
// (`.plan.md`, the agent's "orient" world-model, rendered as markdown). Fetches its own
// `goalDetailQuery`; the `["goals"]` key prefix means the panel's bus invalidation refreshes
// an open drawer live as the loop iterates.
export function GoalDetailDrawer({
  sessionId,
  onClose,
}: {
  sessionId: string | null;
  onClose: () => void;
}) {
  const { data, isLoading, isError } = useQuery({
    ...goalDetailQuery(sessionId ?? ""),
    enabled: !!sessionId,
  });
  const goal = data?.goal ?? null;
  const plan = (data?.plan ?? "").trim();

  const queryClient = useQueryClient();
  const toast = useToast();
  const rearm = useMutation({
    mutationFn: (body: { add_iterations?: number }) => api.rearmGoal(sessionId ?? "", body),
    onSuccess: (res) =>
      toast({ tone: "success", title: "Goal re-armed", message: res.message || "The drive loop will resume." }),
    onError: (e) => toast({ tone: "error", title: "Couldn't re-arm the goal", message: errMsg(e) }),
    // `["goals"]` prefix refreshes both the list and this open drawer's detail.
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.goals }),
  });

  return (
    <Drawer
      open={!!sessionId}
      onClose={onClose}
      width="min(540px, 96vw)"
      className="goal-detail-drawer"
      title={<><Target size={16} /> Goal</>}
      footer={goal ? <GoalActions goal={goal} busy={rearm.isPending} onRearm={(b) => rearm.mutate(b)} /> : undefined}
    >
      {isLoading ? (
        <p className="goal-detail-muted">Loading…</p>
      ) : isError ? (
        <p className="goal-detail-muted" role="alert">Couldn't load this goal.</p>
      ) : !goal ? (
        <p className="goal-detail-muted">This goal is no longer set.</p>
      ) : (
        <GoalDetailBody goal={goal} plan={plan} />
      )}
    </Drawer>
  );
}

// Lifecycle actions (ADR 0079). An ACTIVE goal can be given more room ("+N iterations" — the
// running loop picks up the higher cap). A TERMINAL goal that fell short (exhausted /
// unachievable) can be restarted — the backend resets the loop and kicks a fresh drive turn.
// An achieved goal shows no action (it's done).
function GoalActions({
  goal,
  busy,
  onRearm,
}: {
  goal: GoalState;
  busy: boolean;
  onRearm: (body: { add_iterations?: number }) => void;
}) {
  if (goal.status === "active") {
    return (
      <Button type="button" loading={busy} data-testid="goal-extend" onClick={() => onRearm({ add_iterations: EXTEND_BY })}>
        {busy ? null : <Plus size={15} />} Add {EXTEND_BY} iterations
      </Button>
    );
  }
  if (goal.status === "exhausted" || goal.status === "unachievable") {
    return (
      <Button type="button" variant="primary" loading={busy} data-testid="goal-restart" onClick={() => onRearm({})}>
        {busy ? null : <RotateCcw size={15} />} Restart goal
      </Button>
    );
  }
  return null;
}

function GoalDetailBody({ goal, plan }: { goal: GoalState; plan: string }) {
  const constraints = (goal.constraints ?? []).filter(Boolean);
  const boundaries = (goal.boundaries ?? []).filter(Boolean);
  const hasContract = Boolean(
    goal.outcome || constraints.length || boundaries.length || goal.stop_when,
  );
  const streak = goal.no_progress_streak ?? 0;

  return (
    <div className="goal-detail" data-testid="goal-detail">
      <div className="goal-detail-head">
        <strong className="goal-detail-condition">{goal.condition}</strong>
        <StatusPill label={goal.status} tone={goalTone(goal.status)} />
      </div>

      <dl className="goal-detail-facts">
        <div>
          <dt>Verifier</dt>
          <dd>{verifierLabel(goal.verifier)}</dd>
        </div>
        <div>
          <dt>Progress</dt>
          <dd>
            iteration {goal.iteration ?? 0}/{goal.max_iterations ?? "∞"}
            {streak > 0 ? ` · stalled ${streak}/${goal.no_progress_limit ?? 3}` : ""}
            {goal.fresh_context ? " · fresh-context" : ""}
          </dd>
        </div>
        <div>
          <dt>Session</dt>
          <dd className="goal-detail-mono">{goal.session_id}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>
            {goal.started_at ? ago(goal.started_at) : "—"}
            {goal.finished_at ? ` · finished ${ago(goal.finished_at)}` : ""}
          </dd>
        </div>
      </dl>

      {hasContract ? (
        <section className="goal-detail-section" data-testid="goal-detail-contract">
          <h4>Completion contract</h4>
          {goal.outcome ? (
            <p>
              <span className="goal-detail-key">Outcome:</span> {goal.outcome}
            </p>
          ) : null}
          {constraints.length ? (
            <>
              <p className="goal-detail-key">Constraints (do not violate)</p>
              <ul>{constraints.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </>
          ) : null}
          {boundaries.length ? (
            <>
              <p className="goal-detail-key">Boundaries</p>
              <ul>{boundaries.map((b, i) => <li key={i}>{b}</li>)}</ul>
            </>
          ) : null}
          {goal.stop_when ? (
            <p>
              <span className="goal-detail-key">Stop &amp; ask when:</span> {goal.stop_when}
            </p>
          ) : null}
        </section>
      ) : null}

      {goal.last_reason || goal.last_evidence || goal.abandon_reason ? (
        <section className="goal-detail-section">
          <h4>Last check</h4>
          {goal.last_reason ? <p>{goal.last_reason}</p> : null}
          {goal.abandon_reason ? (
            <p>
              <span className="goal-detail-key">Abandoned:</span> {goal.abandon_reason}
            </p>
          ) : null}
          {goal.last_evidence ? <pre className="goal-detail-evidence">{goal.last_evidence}</pre> : null}
        </section>
      ) : null}

      <section className="goal-detail-section">
        <h4>Plan</h4>
        {plan ? (
          <div className="goal-detail-plan">
            <Markdown>{plan}</Markdown>
          </div>
        ) : (
          <p className="goal-detail-muted">
            No plan recorded yet — the agent maintains one with <code>update_goal_plan</code> as it drives.
          </p>
        )}
      </section>

      {(goal.history?.length ?? 0) > 0 ? (
        <section className="goal-detail-section" data-testid="goal-detail-timeline">
          <h4>Timeline</h4>
          <ol className="goal-timeline">
            {[...(goal.history ?? [])].reverse().map((e, i) => (
              <li className="goal-timeline-item" key={`${e.iteration}-${e.at ?? i}`}>
                <StatusPill label={e.status} tone={goalTone(e.status)} />
                <span className="goal-timeline-body">
                  <span className="goal-timeline-head">
                    <span className="goal-timeline-iter">iter {e.iteration}</span>
                    {e.at ? <span className="goal-timeline-time">{ago(e.at)}</span> : null}
                  </span>
                  {e.reason ? <span className="goal-timeline-reason">{e.reason}</span> : null}
                </span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}
    </div>
  );
}
