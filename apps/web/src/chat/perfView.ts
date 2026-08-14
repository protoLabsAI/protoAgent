// Pure helpers behind the /perf system note (#2677) — kept component-free, same
// convention as promptView.ts, so formatting is unit testable without the vitest
// DOM harness. Reuses TelemetrySummary/TelemetryInsights — the exact data already
// backing the System ▸ Telemetry web dashboard (ADR 0006) and its /api/telemetry/*
// routes — no new aggregation logic, just a chat-native presentation of it.

import { ms, pct, usd } from "../lib/format";
import type { TelemetryInsights, TelemetrySummary } from "../lib/types";

/** "model (N turns, $X.XX)" per by_model row, top N by turn count. Empty string
 *  when there's nothing to show (e.g. a single-model instance with 0 turns). */
export function byModelLine(summary: TelemetrySummary, topN = 3): string {
  const rows = [...summary.by_model].sort((a, b) => b.turns - a.turns).slice(0, topN);
  if (!rows.length) return "";
  return rows.map((r) => `${r.model || "unknown"} (${r.turns} turns, ${usd(r.cost_usd)})`).join(" · ");
}

/** "tool (Np95, N calls)" per by_tool row, top N by p95 duration (#2697) — leads with
 *  the slowest tools, the whole point of this breakdown, rather than most-called (the
 *  by_tool rows already arrive p95-sorted from the server, so this just slices).
 *  Empty string when nothing's been captured yet (an older row / instance). */
export function byToolLine(summary: TelemetrySummary, topN = 3): string {
  const rows = summary.by_tool.slice(0, topN);
  if (!rows.length) return "";
  return rows.map((r) => `${r.tool} (${ms(r.p95_duration_ms)} p95, ${r.calls} calls)`).join(" · ");
}

/** The /perf system note: durable summary stats + live outlier insights,
 *  formatted for chat. `insights` is optional — the note still renders a useful
 *  summary if the insights fetch failed or was skipped, it just omits the
 *  outlier line. */
export function perfNoteMarkdown(summary: TelemetrySummary | null, insights: TelemetryInsights | null): string {
  if (!summary || summary.turns === 0) {
    return "**Performance snapshot**\n\nNo turns recorded yet.";
  }
  const header = `**Performance snapshot** — last ${summary.turns} turn${summary.turns === 1 ? "" : "s"}`;
  const stats = [
    `p50 ${ms(summary.p50_duration_ms)}`,
    `p95 ${ms(summary.p95_duration_ms)}`,
    `success ${pct(summary.success_rate)}`,
    `cache-hit ${pct(summary.cache_hit_ratio)}`,
    `cost ${usd(summary.cost_usd)}`,
  ].join(" · ");
  const byModel = byModelLine(summary);
  const modelLine = byModel ? `\n\n_By model:_ ${byModel}` : "";
  const byTool = byToolLine(summary);
  const toolLine = byTool ? `\n\n_Slowest tools:_ ${byTool}` : "";

  const flagged = insights?.flagged ?? [];
  const outlierLine = flagged.length
    ? `\n\n⚠️ ${flagged.length} outlier turn${flagged.length === 1 ? "" : "s"} flagged — cost or duration far above the recent median.`
    : "";

  return `${header}\n\n${stats}${modelLine}${toolLine}${outlierLine}`;
}
