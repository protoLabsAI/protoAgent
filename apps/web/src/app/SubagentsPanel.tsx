import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense } from "react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { subagentsQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";

// Runtime → Subagents: the delegate roster the lead agent can fan work out to,
// with each one's tools, turn budget, and (if pinned) model override.

function SubagentsBody() {
  const { data } = useSuspenseQuery(subagentsQuery());
  const subagents = data.subagents;

  return (
    <>
      <PanelHeader
        title="Subagents"
        kicker={`${subagents.length} subagent${subagents.length === 1 ? "" : "s"}`}
      />
      <div className="stage-body">
        <div className="subagent-list">
          {subagents.map((subagent) => (
            <div className="subagent-row" key={subagent.name}>
              <div>
                <strong>{subagent.name}</strong>
                <span>{subagent.tools.join(", ") || "no tools"}</span>
              </div>
              <StatusPill label={`${subagent.max_turns} turns`} tone={subagent.enabled ? "success" : "muted"} />
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

export function SubagentsPanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="subagents" />}>
            <Suspense fallback={<PanelSkeleton label="Loading subagents…" />}>
              <SubagentsBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
