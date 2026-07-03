import { useSuspenseQuery } from "@tanstack/react-query";
import { Bot, Database, HardDrive, Settings2, Tag } from "lucide-react";
import { type ReactNode } from "react";

import { brandName } from "../lib/brand";
import { bytes } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { runtimeStatusQuery } from "../lib/queries";
import { StagePanel } from "../app/ErrorBoundary";

// Settings → Overview: the agent's read-only status at a glance (model, knowledge,
// on-disk store sizes). Telemetry is its own tab now. The editable
// bits (name, persona, middleware, tools) live under the Agent section.

// Store sizes can be null while a store is unbuilt — only this surface needs the dash.
const fmtBytes = (n: number | null | undefined): string => (n == null ? "—" : bytes(n));

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
      <PanelHeader
        title="Overview"
        kicker={`${runtime.model?.name || "model not configured"}${runtime.version ? ` · v${runtime.version}` : ""}`}
      />
      <div className="stage-body">
        <div className="metric-grid">
          <Metric icon={<Bot size={16} />} label="Agent" value={brandName(runtime.identity?.name)} />
          <Metric icon={<Tag size={16} />} label="Version" value={runtime.version ? `v${runtime.version}` : "—"} />
          <Metric icon={<Settings2 size={16} />} label="Provider" value={runtime.model?.provider || "none"} />
          <Metric
            icon={<Database size={16} />}
            label="Knowledge"
            value={
              runtime.knowledge.status === "initializing"
                ? "initializing…"
                : runtime.knowledge.resolved_path ||
                  runtime.knowledge.configured_path ||
                  (runtime.knowledge.enabled ? "enabled" : "disabled")
            }
          />
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
    <StagePanel label="overview">
      <StatusBody />
    </StagePanel>
  );
}
