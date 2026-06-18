import "../settings/telemetry.css";

import { Table, THead, TBody, Tr, Th, Td } from "@protolabsai/ui/data";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { useSuspenseQuery } from "@tanstack/react-query";

import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Coins,
  Database,
  Download,
  Hash,
  Layers,
  Wrench,
} from "lucide-react";

import { StagePanel } from "../app/ErrorBoundary";
import { RefreshButton } from "../app/ui-kit";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { QuickSetting } from "../settings/QuickSetting";
import { api } from "../lib/api";
import { ms, pct, tokens, usd } from "../lib/format";
import { telemetryQuery } from "../lib/queries";

// Telemetry dashboard (ADR 0006 Slice 3) — reads /api/telemetry/* (the local
// per-turn rollup store) on the TanStack Query data layer (ADR 0013). Summary
// cards + a recent-turns table; loading via <Suspense>, errors via
// <ErrorBoundary>. Functional: real numbers, theme-consistent, no charts yet.

async function downloadTelemetryCsv() {
  const blob = await api.exportTelemetry();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "telemetry.csv";
  a.click();
  URL.revokeObjectURL(url);
}

function TelemetryBody() {
  const { data, isFetching, refetch } = useSuspenseQuery(telemetryQuery());
  const { enabled, summary, turns, insights } = data;

  return (
    <>
      <PanelHeader
        title="Telemetry"
        kicker={`per-turn cost & latency · ${summary?.turns ?? 0} turns recorded`}
        actions={
          <>
            {/* Quick-set the box-shared telemetry policy (ADR 0048) — both host-scoped,
                so this saves to the host layer. */}
            <QuickSetting keys={["telemetry.enabled", "telemetry.retention_days"]} title="Telemetry" label="Telemetry settings" />
            <Button icon variant="ghost" type="button" onClick={() => void downloadTelemetryCsv()}
                    disabled={!enabled || !summary?.turns} title="Export CSV" data-testid="telemetry-export">
              <Download size={16} />
            </Button>
            <RefreshButton onClick={() => void refetch()} busy={isFetching} />
          </>
        }
      />

      <div className="stage-body">
        {!enabled ? (
          <Empty>Telemetry store is disabled (set <code>telemetry.enabled: true</code>).</Empty>
        ) : !summary || summary.turns === 0 ? (
          <Empty>No turns recorded yet — run a turn and refresh.</Empty>
        ) : (
          <>
            {insights ? (
              <div className="telemetry-insights" data-testid="telemetry-insights">
                <div className={`insight-row ${insights.flagged_count ? "warn" : "ok"}`}>
                  {insights.flagged_count ? (
                    <><AlertTriangle size={15} /> {insights.flagged_count} turn{insights.flagged_count > 1 ? "s" : ""} flagged (≥5× median cost or latency)</>
                  ) : (
                    <><CheckCircle2 size={15} /> No cost or latency outliers</>
                  )}
                </div>
                <div className="insight-row ok">
                  <CheckCircle2 size={15} /> Prompt cache: {pct(insights.levers.cache.hit_ratio)} hit ·
                  ~{usd(insights.levers.cache.est_savings_usd)} saved
                </div>
                {insights.flagged.length ? (
                  <ul className="insight-flags">
                    {insights.flagged.slice(0, 5).map((f) => (
                      <li key={f.task_id}>
                        <span className="flag-when">{(f.ended_at || "").replace("T", " ").slice(5, 19)}</span>
                        <span className="flag-model">{f.model || "—"}</span>
                        <span className="flag-reason">{f.reasons.join(" · ")}</span>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {insights.unproven_levers.length ? (
                  <p className="insight-note">
                    Not yet measured: {insights.unproven_levers.join(", ")}.
                  </p>
                ) : null}
              </div>
            ) : null}

            <div className="metric-grid">
              <Metric icon={<Coins size={16} />} label="Total cost" value={usd(summary.cost_usd)} />
              <Metric icon={<Hash size={16} />} label="Turns" value={String(summary.turns)} />
              <Metric icon={<Activity size={16} />} label="Success" value={pct(summary.success_rate)} />
              <Metric icon={<Database size={16} />} label="Cache hit" value={pct(summary.cache_hit_ratio)} />
              <Metric icon={<Clock size={16} />} label="Latency p50" value={ms(summary.p50_duration_ms)} />
              <Metric icon={<Clock size={16} />} label="Latency p95" value={ms(summary.p95_duration_ms)} />
              <Metric icon={<Layers size={16} />} label="Tokens" value={tokens(summary.total_tokens)} />
              <Metric icon={<Wrench size={16} />} label="Tool calls" value={String(summary.tool_calls)} />
            </div>

            {summary.by_model.length > 0 ? (
              <div className="telemetry-section">
                <h2 className="panel-kicker">By model</h2>
                <Table className="telemetry-table">
                  <THead>
                    <Tr><Th>Model</Th><Th>Turns</Th><Th>Tokens</Th><Th>Cost</Th></Tr>
                  </THead>
                  <TBody>
                    {summary.by_model.map((m) => (
                      <Tr key={m.model || "unknown"}>
                        <Td>{m.model || "—"}</Td>
                        <Td>{m.turns}</Td>
                        <Td>{tokens(m.total_tokens)}</Td>
                        <Td>{usd(m.cost_usd)}</Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              </div>
            ) : null}

            <div className="telemetry-section">
              <h2 className="panel-kicker">Recent turns</h2>
              <Table className="telemetry-table">
                <THead>
                  <Tr>
                    <Th>Ended</Th><Th>Model</Th><Th>Tokens (in→out)</Th>
                    <Th>Cache</Th><Th>Cost</Th><Th>Duration</Th><Th>LLM/Tool</Th><Th>State</Th>
                  </Tr>
                </THead>
                <TBody>
                  {turns.map((t) => (
                    <Tr key={t.task_id} className={t.success ? "" : "turn-failed"}>
                      <Td title={t.ended_at}>{(t.ended_at || "").replace("T", " ").slice(5, 19)}</Td>
                      <Td title={t.models || t.model}>
                        {t.model || "—"}
                        {t.models && t.models.split(",").filter(Boolean).length > 1
                          ? ` +${t.models.split(",").filter(Boolean).length - 1}`
                          : ""}
                      </Td>
                      <Td>{tokens(t.input_tokens)}→{tokens(t.output_tokens)}</Td>
                      <Td>{t.cache_read_input_tokens ? tokens(t.cache_read_input_tokens) : "—"}</Td>
                      <Td>{usd(t.cost_usd)}</Td>
                      <Td>{ms(t.duration_ms)}</Td>
                      <Td>{t.llm_calls}/{t.tool_calls}</Td>
                      <Td><Badge status={t.state === "completed" ? "success" : t.state === "failed" ? "error" : "neutral"}>{t.state}</Badge></Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
            </div>
          </>
        )}
      </div>
    </>
  );
}

export function TelemetrySurface() {
  return (
    <StagePanel label="telemetry" testId="telemetry-surface">
      <TelemetryBody />
    </StagePanel>
  );
}

function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="metric">
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
