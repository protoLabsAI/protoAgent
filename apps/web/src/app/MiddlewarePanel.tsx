import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense } from "react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { runtimeStatusQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";

// Agent → Middleware: the graph middleware wired into each turn (knowledge,
// memory, audit, compaction, …) and whether each is on.

function MiddlewareBody() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  const middleware = Object.entries(runtime.middleware).sort(([a], [b]) => a.localeCompare(b));
  const on = middleware.filter(([, v]) => v).length;

  return (
    <>
      <PanelHeader title="Middleware" kicker={`${on}/${middleware.length} enabled`} />
      <div className="stage-body">
        <div className="table-list">
          {middleware.map(([name, enabled]) => (
            <div className="table-row" key={name}>
              <span>{name}</span>
              <StatusPill label={enabled ? "on" : "off"} tone={enabled ? "success" : "muted"} />
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

export function MiddlewarePanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="middleware" />}>
            <Suspense fallback={<PanelSkeleton label="Loading middleware…" />}>
              <MiddlewareBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
