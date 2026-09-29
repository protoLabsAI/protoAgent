/**
 * Telemetry + observability reads: trajectories, turn telemetry, fleet rollup, activity.
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type {
  ActivityHistory,
  FleetTelemetry,
  TelemetryInsights,
  TelemetrySummary,
  TelemetryTurn,
  TrajectoryCall,
  TrajectoryEvent,
} from "../types";
import { apiUrl, applyAuth } from "./routing";
import { request } from "./http";

export const telemetryApi = {
  trajectoryEvents(sessionId: string, limit = 20) {
    return request<{ found: boolean; events: TrajectoryEvent[]; total: number }>(
      `/api/trajectory/${encodeURIComponent(sessionId)}?limit=${limit}`,
    );
  },

  trajectoryCall(sessionId: string, n: number) {
    return request<TrajectoryCall & { reason?: string }>(
      `/api/trajectory/${encodeURIComponent(sessionId)}/call/${n}`,
    );
  },

  telemetrySummary(since?: string) {
    const q = since ? `?since=${encodeURIComponent(since)}` : "";
    return request<{ enabled: boolean; summary: TelemetrySummary | null }>(
      `/api/telemetry/summary${q}`,
    );
  },

  telemetryRecent(limit = 50) {
    // `langfuse_trace_url_template` carries a `{trace_id}` placeholder (null when
    // Langfuse isn't configured) — see telemetry/traceUrl.ts.
    // `tracing_enabled` says whether Langfuse is on at all (#3017), so a blank Trace
    // cell can say "tracing is off" instead of implying this turn simply wasn't
    // traced. Optional: an older backend omits it and the surface stays as it was.
    return request<{
      enabled: boolean;
      turns: TelemetryTurn[];
      langfuse_trace_url_template?: string | null;
      tracing_enabled?: boolean;
    }>(`/api/telemetry/recent?limit=${limit}`);
  },

  telemetryInsights() {
    return request<{ enabled: boolean; insights: TelemetryInsights | null }>(
      "/api/telemetry/insights",
    );
  },

  // Hub-side fleet telemetry rollup (ADR 0006 fleet extension). Read-only; a
  // single-box install answers with `fleet: false` and just the host member.
  telemetryFleet() {
    return request<FleetTelemetry>("/api/telemetry/fleet");
  },

  // Real completion probe — the true auth check (unlike `models`, which only
  // Download all telemetry as CSV (carries the bearer; returns a Blob to save).
  async exportTelemetry(): Promise<Blob> {
    const res = await fetch(apiUrl("/api/telemetry/export"), {
      headers: applyAuth(new Headers()),
    });
    if (!res.ok) throw new Error(`export failed: ${res.status}`);
    return res.blob();
  },

  activity() {
    return request<ActivityHistory>("/api/activity");
  },
};
