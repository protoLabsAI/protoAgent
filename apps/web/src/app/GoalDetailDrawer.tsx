import { Drawer } from "@protolabsai/ui/overlays";
import { useQuery } from "@tanstack/react-query";
import { Target } from "lucide-react";

import { Markdown } from "../chat/LazyMarkdown";
import { verifierLabel } from "../chat/goalForm";
import { ago } from "../lib/format";
import { goalDetailQuery } from "../lib/queries";
import type { GoalState } from "../lib/types";
import { goalTone } from "./GoalsPanel";
import { StatusPill } from "./StatusPill";

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

  return (
    <Drawer
      open={!!sessionId}
      onClose={onClose}
      width="min(540px, 96vw)"
      className="goal-detail-drawer"
      title={<><Target size={16} /> Goal</>}
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
    </div>
  );
}
