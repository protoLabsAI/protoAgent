import "./tools.css";

import { Input } from "@protolabsai/ui/forms";
import { useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Accordion, AccordionItem, PanelHeader } from "@protolabsai/ui/navigation";
import { Badge } from "@protolabsai/ui/primitives";
import { toolsQuery } from "../lib/queries";
import { StagePanel } from "./ErrorBoundary";

// Runtime → Tools: the live tool inventory the lead agent + subagents can call.
// Core tools group by subsystem; plugin tools group by the PLUGIN that brought them
// (backend stamps the category); MCP tools by server. Order: core subsystems first
// (CORE_ORDER), then plugin groups (alpha), then MCP — so the baseline reads top-down
// and everything an extension adds sits below it. Searchable; a search expands matches.

// Core subsystem order. Plugin/MCP group names are dynamic, so they're NOT listed here —
// they sort after core by source rank (below).
const CORE_ORDER = [
  "General", "Filesystem", "Skills", "Web & research", "Memory", "Scheduler",
  "Inbox", "Tasks", "Goals", "Delegation", "Workflows", "Discovery",
];
// core baseline first, then plugin-contributed, then MCP.
const SOURCE_RANK: Record<string, number> = { core: 0, plugin: 1, mcp: 2 };

function ToolsBody() {
  const { data } = useSuspenseQuery(toolsQuery());
  const [q, setQ] = useState("");
  const query = q.trim().toLowerCase();
  const tools = query
    ? data.tools.filter((t) =>
        `${t.name} ${t.description} ${t.source} ${t.category ?? ""}`.toLowerCase().includes(query))
    : data.tools;

  // Group by category. Each group is homogeneous in source (a core subsystem, one
  // plugin's tools, or MCP), so the group's source = its first tool's.
  const groups = new Map<string, typeof tools>();
  for (const t of tools) {
    const cat = t.category || "General";
    (groups.get(cat) ?? groups.set(cat, []).get(cat)!).push(t);
  }
  const groupSource = (cat: string) => groups.get(cat)![0]?.source ?? "core";
  // Order by source rank (core → plugin → MCP); within core by CORE_ORDER (then alpha
  // for any unlisted core group), within plugin/MCP alphabetically.
  const ordered = [...groups.keys()].sort((a, b) => {
    const sa = SOURCE_RANK[groupSource(a)] ?? 1, sb = SOURCE_RANK[groupSource(b)] ?? 1;
    if (sa !== sb) return sa - sb;
    if (groupSource(a) === "core") {
      const ia = CORE_ORDER.indexOf(a), ib = CORE_ORDER.indexOf(b);
      const ra = ia === -1 ? 99 : ia, rb = ib === -1 ? 99 : ib;
      if (ra !== rb) return ra - rb;
    }
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
                      {/* The group is homogeneous in source, so the source belongs on the
                          GROUP, not repeated on every row. Core is the baseline (no chip);
                          plugin/MCP groups get a chip so what an extension added stands out. */}
                      {groupSource(cat) !== "core" ? (
                        <Badge status="neutral">{groupSource(cat)}</Badge>
                      ) : null}
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
