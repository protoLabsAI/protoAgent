import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense, useState } from "react";

import { PanelHeader } from "./PanelHeader";
import { toolsQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";

// Runtime → Tools: the live tool inventory the lead agent + subagents can call,
// grouped by source (core / plugin / mcp). Searchable — the list grows as you
// add plugins and MCP servers.

const SOURCE_TONE = { core: "success", plugin: "muted", mcp: "muted" } as const;

function ToolsBody() {
  const { data } = useSuspenseQuery(toolsQuery());
  const [q, setQ] = useState("");
  const query = q.trim().toLowerCase();
  const tools = query
    ? data.tools.filter((t) => `${t.name} ${t.description} ${t.source}`.toLowerCase().includes(query))
    : data.tools;

  return (
    <>
      <PanelHeader title="Tools" kicker={`${data.count} wired tool${data.count === 1 ? "" : "s"}`} />
      <div className="stage-body">
        <input
          className="playbook-search"
          type="search"
          placeholder="Search tools…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <div className="table-list">
          {tools.map((t) => (
            <div className="table-row" key={t.name}>
              <span>
                <strong>{t.name}</strong>
                {t.description ? <span className="muted"> — {t.description}</span> : null}
              </span>
              <StatusPill label={t.source} tone={SOURCE_TONE[t.source] ?? "muted"} />
            </div>
          ))}
          {tools.length === 0 ? (
            <div className="table-row"><span>no tools match</span></div>
          ) : null}
        </div>
      </div>
    </>
  );
}

export function ToolsPanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="tools" />}>
            <Suspense fallback={<PanelSkeleton label="Loading tools…" />}>
              <ToolsBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
