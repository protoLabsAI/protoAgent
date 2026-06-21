import "./tools.css";

import { Input } from "@protolabsai/ui/forms";
import { useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Accordion, AccordionItem, PanelHeader } from "@protolabsai/ui/navigation";
import { Badge } from "@protolabsai/ui/primitives";
import { toolsQuery } from "../lib/queries";
import { StagePanel } from "./ErrorBoundary";

// Runtime → Tools: the live tool inventory the lead agent + subagents can call,
// grouped by source (core / plugin / mcp). Searchable — the list grows as you
// add plugins and MCP servers. Each subsystem is a collapsible DS Accordion
// section so a long inventory stays scannable; a search expands every match.

// Subsystem ordering — General leads; integrations next; plugin/MCP last.
const CATEGORY_ORDER = [
  "General", "GitHub", "Notes", "Memory", "Scheduler", "Inbox", "Tasks", "Goals",
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
        {ordered.length ? (
          <Accordion className="tools-groups">
            {ordered.map((cat, i) => {
              const items = groups.get(cat)!;
              return (
                <AccordionItem
                  // Re-key on the query so a search remounts the sections with the
                  // "expand every match" defaultOpen (defaultOpen only applies on mount).
                  key={`${cat}::${query}`}
                  defaultOpen={Boolean(query) || i === 0}
                  title={
                    <span className="tools-group-head">
                      {cat}
                      <Badge status="neutral">{items.length}</Badge>
                    </span>
                  }
                >
                  <div className="tools-list">
                    {items.map((t) => (
                      <div className="tools-row" key={t.name}>
                        <div className="tools-row-main">
                          <code className="tools-name">{t.name}</code>
                          {t.description ? <span className="tools-desc">{t.description}</span> : null}
                        </div>
                        <Badge status={t.source === "core" ? "success" : "neutral"}>{t.source}</Badge>
                      </div>
                    ))}
                  </div>
                </AccordionItem>
              );
            })}
          </Accordion>
        ) : null}
        {tools.length === 0 ? <p className="muted">No tools match.</p> : null}
      </div>
    </>
  );
}

export function ToolsPanel() {
  return (
    <StagePanel label="tools">
      <ToolsBody />
    </StagePanel>
  );
}
