import "../settings/telemetry.css";

import { Table, THead, TBody, Tr, Th, Td } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { useSuspenseQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Coins,
  Copy,
  Database,
  Download,
  ExternalLink,
  Hash,
  Layers,
  Wrench,
} from "lucide-react";

import { StagePanel } from "../app/ErrorBoundary";
import { RefreshButton } from "../app/ui-kit";
import { PanelHeader, Tabs } from "@protolabsai/ui/navigation";
import { QuickSetting } from "../settings/QuickSetting";
import { api } from "../lib/api";
import { localStamp, localStampTitle, ms, pct, tokens, usd } from "../lib/format";
import { fleetTelemetryQuery, telemetryQuery } from "../lib/queries";
import { FleetTelemetrySection } from "./FleetTelemetrySection";
import { defaultTelemetryTab, hasTelemetryViews, telemetryTabItems } from "./telemetryTabs";
import { traceCellState } from "./traceUrl";

import type { TelemetryTabId } from "./telemetryTabs";

import type { TelemetryInsights, TelemetrySummary, TelemetryTurn } from "../lib/types";

// Telemetry dashboard (ADR 0006 Slice 3) — reads /api/telemetry/* (the local
// per-turn rollup store) on the TanStack Query data layer (ADR 0013). Loading via
// <Suspense>, errors via <ErrorBoundary>. Functional: real numbers,
// theme-consistent, no charts yet.
//
// Two layers (#3329). The HEADLINE — insights + the metric cards — is pinned: it is
// why the surface gets opened, and it used to be the only part visible without
// scrolling. The DRILL-DOWNS — recent turns, by model, by tool, and the fleet
// rollup — sit behind a tab strip instead of stacking into one long page; the
// ten-column turns table now gets the height it needs. Which tabs exist and which
// one opens is `telemetryTabs.ts`, so both are unit-testable.

async function downloadTelemetryCsv() {
  const blob = await api.exportTelemetry();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "telemetry.csv";
  a.click();
  URL.revokeObjectURL(url);
}

// What the per-box views say when they have nothing in them. Two reasons, and they
// are not interchangeable: the store is switched off, or it is on and empty. Both
// are reachable while the FLEET tab still has plenty to show.
const NO_TURNS = "No turns recorded yet — run a turn and refresh.";
// A fragment, not a string: the same sentence is rendered from two branches, and a
// plain string drops the <code> the other one has — the setting name is a literal
// the operator has to type.
const STORE_OFF = (
  <>
    Telemetry store is disabled (set <code>telemetry.enabled: true</code>).
  </>
);

function TelemetryBody() {
  const { data, isFetching, refetch } = useSuspenseQuery(telemetryQuery());
  const { data: fleet } = useSuspenseQuery(fleetTelemetryQuery());
  const { enabled, summary, turns, insights, traceUrlTemplate, tracingEnabled } = data;
  const toast = useToast();

  const hasFleet = Boolean(fleet.fleet);
  const hasTurns = Boolean(enabled && summary && summary.turns > 0);
  const tabItems = telemetryTabItems({
    hasModels: Boolean(hasTurns && summary?.by_model.length),
    hasTools: Boolean(hasTurns && summary?.by_tool.length),
    hasFleet,
  });
  const [tab, setTab] = useState<TelemetryTabId>(() => defaultTelemetryTab(hasTurns, hasFleet));
  const active = tabItems.some((t) => t.id === tab) ? tab : tabItems[0].id;
  // Falling back for THIS render isn't enough: leave the dead id in state and the
  // surface snaps back to it the moment that tab returns (the fleet reappearing
  // would yank the operator off whatever they had switched to). Keyed on the ids,
  // not the array, which is rebuilt every render.
  const tabKey = tabItems.map((t) => t.id).join(",");
  useEffect(() => {
    if (active !== tab) setTab(active);
  }, [tabKey, active, tab]);

  return (
    <>
      <PanelHeader
        title="Telemetry"
        kicker={`per-turn cost & latency · ${summary?.turns ?? 0} turns recorded`}
        actions={
          <>
            {/* Keep each shortcut single-scope (#3032): QuickSetting deliberately makes one
                settings write. These five are box-shared, while fleet trace export below is
                the focused agent's own toggle. */}
            <QuickSetting
              keys={[
                "telemetry.enabled",
                "telemetry.retention_days",
                "prompts.capture",
                "prompts.retention_days",
                "prompts.max_calls",
              ]}
              title="Telemetry & prompt capture"
              label="Telemetry and prompt capture settings"
            />
            <QuickSetting
              keys={["telemetry.fleet_trace_export"]}
              title="Fleet trace export"
              label="Fleet trace export setting"
              icon={<Layers size={15} />}
            />
            {/* Tracing's shortcut to the same four fields Settings ▸ Tracing owns (#3017), so
                both halves of ADR 0006 are one click apart from the column that reports them.
                A SEPARATE chip rather than four more keys on the one above: QuickSetting saves
                to the host layer only when EVERY key it edits is host-scoped, and tracing.* is
                agent-scoped — folding them together would have quietly demoted telemetry.enabled
                to the agent leaf. */}
            <QuickSetting
              keys={["tracing.enabled", "tracing.host", "tracing.public_key", "tracing.secret_key"]}
              title="Langfuse tracing"
              label="Langfuse tracing settings"
              icon={<Activity size={15} />}
            />
            <Button icon variant="ghost" type="button" onClick={() => void downloadTelemetryCsv()}
                    disabled={!enabled || !summary?.turns} title="Export CSV" data-testid="telemetry-export">
              <Download size={16} />
            </Button>
            <RefreshButton onClick={() => void refetch()} busy={isFetching} />
          </>
        }
      />

      <div className="stage-body">
        {/* The fleet rollup is INDEPENDENT of this box's own store: a hub can have
            telemetry switched off entirely while its members are busy. Gating the
            whole surface on `enabled` hid it — the regression this shape avoids. */}
        {!hasTelemetryViews(hasTurns, hasFleet) ? (
          <Empty>{enabled ? NO_TURNS : STORE_OFF}</Empty>
        ) : (
          <>
            {/* The pinned headline: what the surface is opened FOR. Absent when this box
                has no turns of its own — a fleet hub still gets the Fleet tab. */}
            {hasTurns && insights ? <InsightsBlock insights={insights} /> : null}
            {hasTurns && summary ? <MetricGrid summary={summary} /> : null}

            {/* The drill-downs. Not `responsive`: four short labels fit the dialog, and a
                deterministic role="tab" strip keeps the e2e + a11y contract simple (the
                same call MemorySurface made). Labelled, because the Settings sidenav
                around it is itself a tablist. */}
            <div className="telemetry-views" data-testid="telemetry-views">
              <Tabs
                active={active}
                onSelect={(t) => setTab(t as TelemetryTabId)}
                items={tabItems}
                ariaLabel="Telemetry views"
              />
            </div>

            {active === "fleet" ? (
              <FleetTelemetrySection fleet={fleet} />
            ) : !hasTurns || !summary ? (
              <Empty className="telemetry-empty">{enabled ? NO_TURNS : STORE_OFF}</Empty>
            ) : active === "models" ? (
              <ByModelPanel rows={summary.by_model} />
            ) : active === "tools" ? (
              <ByToolPanel rows={summary.by_tool} />
            ) : (
              <RecentTurnsPanel
                turns={turns}
                traceUrlTemplate={traceUrlTemplate}
                tracingEnabled={tracingEnabled}
                onTraceCopied={() =>
                  toast({ title: "Trace id copied", message: "Paste it into Langfuse's search." })}
              />
            )}
          </>
        )}
      </div>
    </>
  );
}

// The advise-only insights block (Slice 4): outliers against that model's own median,
// plus the one lever measured rather than assumed.
function InsightsBlock({ insights }: { insights: TelemetryInsights }) {
  return (
    <div className="telemetry-insights" data-testid="telemetry-insights">
      <div className={`insight-row ${insights.flagged_count ? "warn" : "ok"}`}>
        {insights.flagged_count ? (
          <><AlertTriangle size={15} /> {insights.flagged_count} turn{insights.flagged_count > 1 ? "s" : ""} flagged (≥5× that model's median cost or latency)</>
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
          {insights.flagged.slice(0, 5).map((f, i) => (
            <li key={f.row_id ?? `${f.task_id}-${i}`}>
              <span className="flag-when" title={localStampTitle(f.ended_at)}>{localStamp(f.ended_at)}</span>
              <span className="flag-model">{f.model || "—"}</span>
              <span className="flag-reason">{f.reasons.join(" · ")}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {insights.unproven_levers.length ? (
        <p className="insight-note">Not yet measured: {insights.unproven_levers.join(", ")}.</p>
      ) : null}
    </div>
  );
}

// The pinned metric cards. `telemetry-metrics` densifies the SHARED `.metric-grid`
// for this surface only — see telemetry.css.
function MetricGrid({ summary }: { summary: TelemetrySummary }) {
  return (
    <div className="metric-grid telemetry-metrics" data-testid="telemetry-metrics">
      <Metric icon={<Coins size={16} />} label="Total cost" value={usd(summary.cost_usd)} />
      <Metric icon={<Hash size={16} />} label="Turns" value={String(summary.turns)} />
      <Metric icon={<Activity size={16} />} label="Success" value={pct(summary.success_rate)} />
      <Metric icon={<Database size={16} />} label="Cache hit" value={pct(summary.cache_hit_ratio)} />
      <Metric icon={<Clock size={16} />} label="Latency p50" value={ms(summary.p50_duration_ms)} />
      <Metric icon={<Clock size={16} />} label="Latency p95" value={ms(summary.p95_duration_ms)} />
      <Metric icon={<Clock size={16} />} label="Latency p99" value={ms(summary.p99_duration_ms)} />
      <Metric icon={<Layers size={16} />} label="Tokens" value={tokens(summary.total_tokens)} />
      {summary.p95_context_tokens ? (
        <Metric icon={<Layers size={16} />} label="Context p95" value={tokens(summary.p95_context_tokens)} />
      ) : null}
      {summary.max_context_tokens ? (
        <Metric icon={<Layers size={16} />} label="Context peak" value={tokens(summary.max_context_tokens)} />
      ) : null}
      <Metric icon={<Wrench size={16} />} label="Tool calls" value={String(summary.tool_calls)} />
    </div>
  );
}

function ByModelPanel({ rows }: { rows: TelemetrySummary["by_model"] }) {
  if (!rows.length) return <Empty className="telemetry-empty">No per-model rollup yet.</Empty>;
  return (
    <div className="telemetry-section" data-testid="telemetry-by-model">
      <Table className="telemetry-table">
        <THead>
          <Tr><Th>Model</Th><Th>Turns</Th><Th>Tokens</Th><Th>Cost</Th><Th>p50</Th><Th>p95</Th><Th>p99</Th></Tr>
        </THead>
        <TBody>
          {rows.map((m) => (
            <Tr key={m.model || "unknown"}>
              <Td>{m.model || "—"}</Td>
              <Td>{m.turns}</Td>
              <Td>{tokens(m.total_tokens)}</Td>
              <Td>{usd(m.cost_usd)}</Td>
              <Td>{ms(m.p50_duration_ms)}</Td>
              <Td>{ms(m.p95_duration_ms)}</Td>
              <Td>{ms(m.p99_duration_ms)}</Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

function ByToolPanel({ rows }: { rows: TelemetrySummary["by_tool"] }) {
  if (!rows.length) return <Empty className="telemetry-empty">No tool calls recorded yet.</Empty>;
  return (
    <div className="telemetry-section" data-testid="telemetry-by-tool">
      <Table className="telemetry-table">
        <THead>
          <Tr><Th>Tool</Th><Th>Calls</Th><Th>p50</Th><Th>p95</Th><Th>p99</Th></Tr>
        </THead>
        <TBody>
          {rows.map((t) => (
            <Tr key={t.tool}>
              <Td>{t.tool}</Td>
              <Td>{t.calls}</Td>
              <Td>{ms(t.p50_duration_ms)}</Td>
              <Td>{ms(t.p95_duration_ms)}</Td>
              <Td>{ms(t.p99_duration_ms)}</Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

function RecentTurnsPanel({
  turns,
  traceUrlTemplate,
  tracingEnabled,
  onTraceCopied,
}: {
  turns: TelemetryTurn[];
  traceUrlTemplate: string | null;
  tracingEnabled: boolean;
  onTraceCopied: () => void;
}) {
  if (!turns.length) return <Empty className="telemetry-empty">{NO_TURNS}</Empty>;
  return (
    <div className="telemetry-section" data-testid="telemetry-turns">
      <Table className="telemetry-table">
        <THead>
          <Tr>
            <Th>Ended</Th><Th>Model</Th><Th>Tokens (in→out)</Th>
            <Th>Context</Th><Th>Cache</Th><Th>Cost</Th><Th>Duration</Th><Th>LLM/Tool</Th><Th>State</Th><Th>Trace</Th>
          </Tr>
        </THead>
        <TBody>
          {turns.map((t, i) => (
            <Tr key={t.row_id ?? `${t.task_id}-${i}`} className={t.success ? "" : "turn-failed"}>
              <Td title={localStampTitle(t.ended_at)}>{localStamp(t.ended_at)}</Td>
              <Td title={t.models || t.model}>
                {t.model || "—"}
                {t.models && t.models.split(",").filter(Boolean).length > 1
                  ? ` +${t.models.split(",").filter(Boolean).length - 1}`
                  : ""}
              </Td>
              <Td>{tokens(t.input_tokens)}→{tokens(t.output_tokens)}</Td>
              <Td title="Peak single-call prompt size — the context-window fill this turn reached (#2773)">
                {t.context_tokens ? tokens(t.context_tokens) : "—"}
              </Td>
              <Td title={t.cache_hit_ratio != null ? `${Math.round(t.cache_hit_ratio * 100)}% of this turn's prompt tokens were cache reads` : undefined}>
                {t.cache_read_input_tokens ? tokens(t.cache_read_input_tokens) : "—"}
                {t.cache_hit_ratio ? ` (${Math.round(t.cache_hit_ratio * 100)}%)` : ""}
              </Td>
              <Td>{usd(t.cost_usd)}</Td>
              <Td>{ms(t.duration_ms)}</Td>
              <Td>{t.llm_calls}/{t.tool_calls}</Td>
              <Td><Badge status={t.state === "completed" ? "success" : t.state === "failed" ? "error" : "neutral"}>{t.state}</Badge></Td>
              <Td><TraceCell turn={t} template={traceUrlTemplate} tracingEnabled={tracingEnabled} onCopied={onTraceCopied} /></Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

export function TelemetrySurface() {
  return (
    <StagePanel label="telemetry" testId="telemetry-surface">
      <TelemetryBody />
    </StagePanel>
  );
}

// Pivot from a telemetry row to its Langfuse trace. With a server-supplied URL
// template that's a deep link; without one (Langfuse configured but its project
// id unreachable, or no template at all) it degrades to a copyable trace id —
// an honest id beats a fabricated link. Rows traced before the column existed show "—".
//
// With Langfuse OFF the cell says so (#3017). Every row is blank in that case, and a
// column of dashes reads as "these particular turns weren't traced" — which is how a
// fleet ran 336 turns with tracing dark and nothing in the product said so.
// The four-way decision itself lives in traceCellState (traceUrl.ts) so it can be
// unit-tested — the console has no component-rendering suite.
function TraceCell({
  turn,
  template,
  tracingEnabled,
  onCopied,
}: {
  turn: TelemetryTurn;
  template: string | null;
  tracingEnabled: boolean;
  onCopied: () => void;
}) {
  const state = traceCellState(template, turn.trace_id, tracingEnabled);

  switch (state.kind) {
    case "link":
      return (
        <a className="trace-link" href={state.href} target="_blank" rel="noreferrer noopener"
           title={`Open trace ${turn.trace_id} in Langfuse`} data-testid="telemetry-trace-link">
          <ExternalLink size={13} aria-hidden /> {state.short}
        </a>
      );
    case "copy":
      return (
        <Button type="button" variant="ghost" size="sm" className="trace-copy"
                title={`Copy trace id ${state.traceId}`} data-testid="telemetry-trace-copy"
                onClick={() => { void navigator.clipboard?.writeText(state.traceId); onCopied(); }}>
          <Copy size={13} aria-hidden /> {state.short}
        </Button>
      );
    case "off":
      // The half-finished setup lands here too (toggle on, keys stored blank), so the
      // title names what it takes rather than just "turn it on" — the same distinction
      // observability/tracing.py draws in its boot log. It names Settings ▸ Tracing, the
      // section that actually renders these fields in every console window; the gear beside
      // this table is the same four fields (#3017).
      return (
        <span className="trace-none" data-testid="telemetry-trace-off"
              title="Tracing is disabled on this agent — Settings ▸ Tracing ▸ Send traces to Langfuse needs the toggle AND both Langfuse keys">
          off
        </span>
      );
    default:
      return <span className="trace-none">—</span>;
  }
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
