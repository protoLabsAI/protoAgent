import type { TelemetryByModelRow, TelemetryInsights } from "../lib/types";

// Per-lane cache reporting (#3342). Extracted from TelemetrySurface for the same
// reason telemetryTabs was: the judgement is the part worth testing, and it can't be
// reached through a .tsx helper.

/** Name the lanes that aren't caching.
 *
 * `cache.model` is the DOMINANT model, which on a mixed agent is usually the lane
 * caching FINE — naming it would accuse the wrong one. It is therefore only the
 * fallback for a pre-#3342 backend (a fleet member on an older release) that sends
 * no `cold_lanes` at all.
 */
export function coldLaneLabel(cache: TelemetryInsights["levers"]["cache"]): string {
  const named = (cache.cold_lanes ?? []).map((l) => l.model || "unknown");
  if (named.length) return named.join(", ");
  return cache.model || "this agent";
}

/** This lane's cache-hit ratio, or "—" when there is nothing to judge.
 *
 * A lane that recorded no prompt tokens is UNMEASURED, not uncached: an ACP coder leg
 * runs outside the gateway and reports no usage at all (#3015). Printing 0% for it
 * reads as a defect in a lane nobody can fix from here.
 */
export function laneCacheHit(m: TelemetryByModelRow, pct: (n: number) => string): string {
  const prompt =
    (m.input_tokens ?? 0) + (m.cache_read_input_tokens ?? 0) + (m.cache_creation_input_tokens ?? 0);
  if (m.cache_hit_ratio === undefined || prompt <= 0) return "—";
  return pct(m.cache_hit_ratio);
}
