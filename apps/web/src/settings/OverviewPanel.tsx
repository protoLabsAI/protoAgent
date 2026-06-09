import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Bot, Database, HardDrive, Settings2, Sparkles } from "lucide-react";
import { Suspense, type ReactNode } from "react";

import { brandName } from "../lib/brand";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { runtimeStatusQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";

// Settings → Overview: the agent's read-only status at a glance (model, knowledge,
// goal, on-disk store sizes). Telemetry is its own tab now. The editable
// bits (name, persona, middleware, tools) live under the Agent section.

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

function StatusBody() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  return (
    <>
      <PanelHeader title="Overview" kicker={runtime.model?.name || "model not configured"} />
      <div className="stage-body">
        <div className="metric-grid">
          <Metric icon={<Bot size={16} />} label="Agent" value={brandName(runtime.identity?.name)} />
          <Metric icon={<Settings2 size={16} />} label="Provider" value={runtime.model?.provider || "none"} />
          <Metric icon={<Database size={16} />} label="Knowledge" value={runtime.knowledge.resolved_path || runtime.knowledge.configured_path || "disabled"} />
          <Metric icon={<Sparkles size={16} />} label="Goal mode" value={runtime.goal.enabled ? "on" : "off"} />
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
      </div>
    </>
  );
}

export function OverviewPanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="overview" />}>
            <Suspense fallback={<PanelSkeleton label="Loading overview…" />}>
              <StatusBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
