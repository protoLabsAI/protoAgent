import {
  Activity,
  Clock,
  Coins,
  Database,
  Hash,
  Layers,
  RefreshCw,
  Wrench,
} from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "../lib/api";
import type { TelemetrySummary, TelemetryTurn } from "../lib/types";

// Telemetry dashboard (ADR 0006 Slice 3) — reads /api/telemetry/* (the local
// per-turn rollup store). Summary cards + a recent-turns table. Functional
// first: real numbers, theme-consistent, no charts yet.

function usd(n: number): string {
  if (!n) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function ms(n: number): string {
  if (!n) return "—";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`;
}

function pct(n: number): string {
  return `${Math.round((n || 0) * 100)}%`;
}

export function TelemetrySurface({ onError }: { onError: (message: string) => void }) {
  const [summary, setSummary] = useState<TelemetrySummary | null>(null);
  const [turns, setTurns] = useState<TelemetryTurn[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const [s, r] = await Promise.all([api.telemetrySummary(), api.telemetryRecent(50)]);
      setEnabled(s.enabled && r.enabled);
      setSummary(s.summary);
      setTurns(r.turns || []);
      onError("");
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  return (
    <section className="panel stage-panel" data-testid="telemetry-surface">
      <div className="panel-header">
        <div>
          <h1>Telemetry</h1>
          <p className="panel-kicker">per-turn cost &amp; latency · {summary?.turns ?? 0} turns recorded</p>
        </div>
        <button className="secondary-button" type="button" onClick={() => void load()} disabled={loading} title="Refresh">
          <RefreshCw size={15} className={loading ? "spin" : ""} /> Refresh
        </button>
      </div>

      <div className="stage-body">
        {!enabled ? (
          <p className="empty-note">Telemetry store is disabled (set <code>telemetry.enabled: true</code>).</p>
        ) : !summary || summary.turns === 0 ? (
          <p className="empty-note">No turns recorded yet — run a turn and refresh.</p>
        ) : (
          <>
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
                <table className="telemetry-table">
                  <thead>
                    <tr><th>Model</th><th>Turns</th><th>Tokens</th><th>Cost</th></tr>
                  </thead>
                  <tbody>
                    {summary.by_model.map((m) => (
                      <tr key={m.model || "unknown"}>
                        <td>{m.model || "—"}</td>
                        <td>{m.turns}</td>
                        <td>{tokens(m.total_tokens)}</td>
                        <td>{usd(m.cost_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}

            <div className="telemetry-section">
              <h2 className="panel-kicker">Recent turns</h2>
              <table className="telemetry-table">
                <thead>
                  <tr>
                    <th>Ended</th><th>Model</th><th>Tokens (in→out)</th>
                    <th>Cache</th><th>Cost</th><th>Duration</th><th>LLM/Tool</th><th>State</th>
                  </tr>
                </thead>
                <tbody>
                  {turns.map((t) => (
                    <tr key={t.task_id} className={t.success ? "" : "turn-failed"}>
                      <td title={t.ended_at}>{(t.ended_at || "").replace("T", " ").slice(5, 19)}</td>
                      <td>{t.model || "—"}</td>
                      <td>{tokens(t.input_tokens)}→{tokens(t.output_tokens)}</td>
                      <td>{t.cache_read_input_tokens ? tokens(t.cache_read_input_tokens) : "—"}</td>
                      <td>{usd(t.cost_usd)}</td>
                      <td>{ms(t.duration_ms)}</td>
                      <td>{t.llm_calls}/{t.tool_calls}</td>
                      <td><span className={`turn-state turn-state-${t.state}`}>{t.state}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </section>
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
