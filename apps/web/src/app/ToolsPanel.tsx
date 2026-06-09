import { Input } from "@protolabsai/ui/forms";
import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense, useState } from "react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { toolsQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";

// Runtime → Tools: the live tool inventory the lead agent + subagents can call,
// grouped by source (core / plugin / mcp). Searchable — the list grows as you
// add plugins and MCP servers.

const SOURCE_TONE = { core: "success", plugin: "muted", mcp: "muted" } as const;

// Subsystem ordering — General leads; integrations next; plugin/MCP last.
const CATEGORY_ORDER = [
  "General", "GitHub", "Notes", "Memory", "Scheduler", "Inbox", "Beads", "Goals",
  "Delegation", "Workflows", "Discovery", "Plugin", "MCP",
];

function ToolsBody() {
  const { data } = useSuspenseQuery(toolsQuery());
  const [q, setQ] = useState("");
  const query = q.trim().toLowerCase();
  const tools = query
    ? data.tools.filter((t) =>
        `${t.name} ${t.description} ${t.source} ${t.category ?? ""}`.toLowerCase().includes(query))
    : data.tools;

  // Group by category, then order the groups (known order first, unknowns alpha).
  const groups = new Map<string, typeof tools>();
  for (const t of tools) {
    const cat = t.category || "General";
    (groups.get(cat) ?? groups.set(cat, []).get(cat)!).push(t);
  }
  const ordered = [...groups.keys()].sort((a, b) => {
    const ia = CATEGORY_ORDER.indexOf(a), ib = CATEGORY_ORDER.indexOf(b);
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.localeCompare(b);
  });

  return (
    <>
      <PanelHeader title="Tools" kicker={`${data.count} wired tool${data.count === 1 ? "" : "s"} · ${groups.size} group${groups.size === 1 ? "" : "s"}`} />
      <div className="stage-body">
        <Input
          className="playbook-search"
          type="search"
          placeholder="Search tools…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {ordered.map((cat) => (
          <div key={cat}>
            <p className="panel-kicker">{cat} <span className="muted">· {groups.get(cat)!.length}</span></p>
            <div className="table-list">
              {groups.get(cat)!.map((t) => (
                <div className="table-row" key={t.name}>
                  <span>
                    <strong>{t.name}</strong>
                    {t.description ? <span className="muted"> — {t.description}</span> : null}
                  </span>
                  <StatusPill label={t.source} tone={SOURCE_TONE[t.source] ?? "muted"} />
                </div>
              ))}
            </div>
          </div>
        ))}
        {tools.length === 0 ? <p className="muted">No tools match.</p> : null}
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
