import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Bot, Database, Settings2, Sparkles } from "lucide-react";
import { Suspense, type ReactNode } from "react";

import { brandName } from "../lib/brand";
import { runtimeStatusQuery, subagentsQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";

// System → Runtime: the agent's configured surface (model, middleware, skills,
// MCP servers, plugins, subagents). On the TanStack Query data layer (ADR 0013)
// via useSuspenseQuery on the same `runtime` key the shell reads non-suspense.

function formatBool(value: boolean) {
  return value ? "on" : "off";
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="metric">
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function RuntimeBody() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  const { data: subData } = useSuspenseQuery(subagentsQuery());
  const subagents = subData.subagents;
  const middleware = Object.entries(runtime.middleware).sort(([a], [b]) => a.localeCompare(b));

  return (
    <>
      <div className="panel-header">
        <div>
          <h1>Runtime</h1>
          <p className="panel-kicker">{runtime.model?.name || "model not configured"}</p>
        </div>
        <StatusPill label={runtime.scheduler.backend || "scheduler"} tone="muted" />
      </div>
      <div className="stage-body">
        <div className="metric-grid">
          <Metric icon={<Bot size={16} />} label="Agent" value={brandName(runtime.identity?.name)} />
          <Metric icon={<Settings2 size={16} />} label="Provider" value={runtime.model?.provider || "none"} />
          <Metric icon={<Database size={16} />} label="Knowledge" value={runtime.knowledge.resolved_path || runtime.knowledge.configured_path || "disabled"} />
          <Metric icon={<Sparkles size={16} />} label="Goal mode" value={formatBool(Boolean(runtime.goal.enabled))} />
        </div>
        <p className="panel-kicker">Middleware</p>
        <div className="table-list">
          {middleware.map(([name, enabled]) => (
            <div className="table-row" key={name}>
              <span>{name}</span>
              <StatusPill label={formatBool(enabled)} tone={enabled ? "success" : "muted"} />
            </div>
          ))}
        </div>

        <p className="panel-kicker">Skills</p>
        <div className="table-list">
          <div className="table-row">
            <span>SKILL.md skills loaded</span>
            <StatusPill
              label={`${runtime.skills?.count ?? 0}`}
              tone={(runtime.skills?.count ?? 0) > 0 ? "success" : "muted"}
            />
          </div>
        </div>

        <p className="panel-kicker">MCP servers</p>
        <div className="table-list">
          {runtime.mcp?.servers?.length ? (
            runtime.mcp.servers.map((server) => (
              <div className="table-row" key={server.name}>
                <span>{server.name} · {server.transport}</span>
                <StatusPill label={`${server.tool_count} tool${server.tool_count === 1 ? "" : "s"}`} tone="success" />
              </div>
            ))
          ) : (
            <div className="table-row">
              <span>no MCP servers</span>
              <StatusPill label={runtime.mcp?.enabled ? "enabled" : "off"} tone="muted" />
            </div>
          )}
        </div>

        <p className="panel-kicker">Plugins</p>
        <div className="table-list">
          {runtime.plugins?.length ? (
            runtime.plugins.map((plugin) => (
              <div className="table-row" key={plugin.id}>
                <span>
                  {plugin.name}
                  {plugin.loaded && plugin.tools.length ? ` · ${plugin.tools.length} tool${plugin.tools.length === 1 ? "" : "s"}` : ""}
                  {plugin.loaded && plugin.skills ? ` · ${plugin.skills} skill${plugin.skills === 1 ? "" : "s"}` : ""}
                  {plugin.error ? ` · ${plugin.error}` : ""}
                </span>
                <StatusPill
                  label={plugin.loaded ? "loaded" : plugin.error ? "error" : plugin.enabled ? "enabled" : "disabled"}
                  tone={plugin.loaded ? "success" : plugin.error ? "error" : "muted"}
                />
              </div>
            ))
          ) : (
            <div className="table-row">
              <span>no plugins</span>
              <StatusPill label="none" tone="muted" />
            </div>
          )}
        </div>

        <p className="panel-kicker">Subagents</p>
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

export function RuntimePanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="runtime" />}>
            <Suspense fallback={<PanelSkeleton label="Loading runtime…" />}>
              <RuntimeBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
