import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Bot, Database, HardDrive, Settings2, Sparkles } from "lucide-react";
import { Suspense, type ReactNode } from "react";

import { brandName } from "../lib/brand";
import { PanelHeader } from "./PanelHeader";
import { runtimeStatusQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";

// Runtime → Overview: the agent's configured surface at a glance (model,
// middleware, storage, skills). Tools / MCP / Subagents are their own tabs.
// On the TanStack Query data layer (ADR 0013) via useSuspenseQuery on the same
// `runtime` key the shell reads non-suspense.

function formatBool(value: boolean) {
  return value ? "on" : "off";
}

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
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
  const middleware = Object.entries(runtime.middleware).sort(([a], [b]) => a.localeCompare(b));

  return (
    <>
      <PanelHeader
        title="Runtime"
        kicker={runtime.model?.name || "model not configured"}
        actions={<StatusPill label={runtime.scheduler.backend || "scheduler"} tone="muted" />}
      />
      <div className="stage-body">
        <div className="metric-grid">
          <Metric icon={<Bot size={16} />} label="Agent" value={brandName(runtime.identity?.name)} />
          <Metric icon={<Settings2 size={16} />} label="Provider" value={runtime.model?.provider || "none"} />
          <Metric icon={<Database size={16} />} label="Knowledge" value={runtime.knowledge.resolved_path || runtime.knowledge.configured_path || "disabled"} />
          <Metric icon={<Sparkles size={16} />} label="Goal mode" value={formatBool(Boolean(runtime.goal.enabled))} />
        </div>
        {runtime.storage ? (
          <>
            <p className="panel-kicker">
              Storage{runtime.storage.telemetry_retention_days ? ` · telemetry kept ${runtime.storage.telemetry_retention_days}d` : ""}
            </p>
            <div className="metric-grid">
              <Metric icon={<HardDrive size={16} />} label="Knowledge DB" value={fmtBytes(runtime.storage.knowledge_bytes)} />
              <Metric icon={<HardDrive size={16} />} label="Telemetry DB" value={fmtBytes(runtime.storage.telemetry_bytes)} />
              <Metric icon={<HardDrive size={16} />} label="Checkpoints DB" value={fmtBytes(runtime.storage.checkpoint_bytes)} />
              <Metric icon={<HardDrive size={16} />} label="Skills DB" value={fmtBytes(runtime.storage.skills_bytes)} />
            </div>
          </>
        ) : null}
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
