import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense } from "react";

import { PanelHeader } from "./PanelHeader";
import { runtimeStatusQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";

// Runtime → MCP: external Model Context Protocol servers whose tools are wired
// into the agent (namespaced <server>__<tool>).

function McpBody() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  const servers = runtime.mcp?.servers ?? [];
  const total = runtime.mcp?.tool_count ?? 0;

  return (
    <>
      <PanelHeader
        title="MCP servers"
        kicker={`${servers.length} server${servers.length === 1 ? "" : "s"} · ${total} tool${total === 1 ? "" : "s"}`}
      />
      <div className="stage-body">
        <div className="table-list">
          {servers.length ? (
            servers.map((server) => (
              <div className="table-row" key={server.name}>
                <span>{server.name} · {server.transport}</span>
                <StatusPill label={`${server.tool_count} tool${server.tool_count === 1 ? "" : "s"}`} tone="success" />
              </div>
            ))
          ) : (
            <div className="table-row">
              <span>no MCP servers configured</span>
              <StatusPill label={runtime.mcp?.enabled ? "enabled" : "off"} tone="muted" />
            </div>
          )}
        </div>
      </div>
    </>
  );
}

export function McpPanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="MCP" />}>
            <Suspense fallback={<PanelSkeleton label="Loading MCP…" />}>
              <McpBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
