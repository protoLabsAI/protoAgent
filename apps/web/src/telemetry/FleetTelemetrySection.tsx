import { Table, THead, TBody, Tr, Th, Td } from "@protolabsai/ui/data";
import { Badge } from "@protolabsai/ui/primitives";
import { AlertTriangle, ExternalLink } from "lucide-react";

import { localStamp, localStampTitle, ms, pct, usd } from "../lib/format";
import { langfuseTraceUrl } from "./traceUrl";
import { fleetFlagRows, memberBadge, sortFleetMembers } from "./fleetRollup";

import type { FleetTelemetry } from "../lib/types";

// The Fleet section of the core Telemetry surface (ADR 0006 fleet extension, Slice
// 2): the hub-side rollup across fleet members, plus their flagged problems with
// evidence. READ-ONLY — reachability and metrics only, no controls that
// change/restart/mutate anything. A single-box install (no peers) renders nothing,
// leaving today's per-instance view unchanged.
export function FleetTelemetrySection({ fleet }: { fleet: FleetTelemetry }) {
  if (!fleet.fleet) return null;

  const members = sortFleetMembers(fleet.members);
  const flagRows = fleetFlagRows(fleet.members);
  const template = fleet.langfuse_trace_url_template;

  return (
    <section className="telemetry-section fleet-telemetry" data-testid="fleet-telemetry">
      <h2 className="panel-kicker">Fleet</h2>

      {/* One row per member: reachability/running + the turns/cost/success/cache-hit
          rollup. No action controls — this is a read-only observability grid. */}
      <Table className="telemetry-table fleet-members">
        <THead>
          <Tr>
            <Th>Member</Th><Th>Status</Th><Th>Turns</Th>
            <Th>Cost</Th><Th>Success</Th><Th>Cache hit</Th>
          </Tr>
        </THead>
        <TBody>
          {members.map((m) => {
            const badge = memberBadge(m);
            return (
              <Tr key={m.slug} className={m.reachable ? "" : "member-unreachable"}>
                <Td>
                  {/* testid on the native span (not the DS <Tr>) so the member name
                      is queryable regardless of DS prop forwarding. */}
                  <span className="member-name" data-testid={`fleet-member-name-${m.slug}`}>{m.label}</span>
                  {m.host ? <Badge status="neutral">this instance</Badge> : null}
                  {m.remote ? <Badge status="neutral">remote</Badge> : null}
                </Td>
                <Td><Badge status={badge.status}>{badge.label}</Badge></Td>
                {m.reachable && m.rollup ? (
                  <>
                    <Td>{m.rollup.turns}</Td>
                    <Td>{usd(m.rollup.cost_usd)}</Td>
                    <Td>{pct(m.rollup.success_rate)}</Td>
                    <Td>{pct(m.rollup.cache_hit_ratio)}</Td>
                  </>
                ) : (
                  <Td colSpan={4} className="member-note">
                    {m.reachable ? "telemetry off" : "did not respond"}
                  </Td>
                )}
              </Tr>
            );
          })}
        </TBody>
      </Table>

      {/* Flagged problems across the fleet, each carrying its evidence: member,
          per-turn row, trace_id, resolved trace link, timestamp. The member column
          is the resolved LABEL (never the routing slug) — see fleetFlagRows. */}
      {flagRows.length ? (
        <div className="fleet-flags" data-testid="fleet-flags">
          <h3 className="panel-kicker">
            <AlertTriangle size={13} aria-hidden /> Flagged problems
          </h3>
          <ul className="insight-flags">
            {flagRows.map((row, i) => {
              const turn = row.evidence.turn;
              const traceId = (row.evidence.trace_id || "").trim();
              const href = langfuseTraceUrl(template, traceId);
              const ts = row.evidence.timestamp;
              return (
                <li key={`${row.slug}-${turn.row_id ?? turn.task_id ?? i}-${i}`} data-testid="fleet-flag">
                  <span className="flag-member" data-testid="fleet-flag-member">{row.memberLabel}</span>
                  <span className="flag-when" title={localStampTitle(ts)}>{localStamp(ts)}</span>
                  <span className="flag-model">{turn.model || "—"}</span>
                  {/* Per-turn evidence — the concrete numbers that tripped the signal. */}
                  <span className="flag-turn">
                    {turn.cost_usd != null ? usd(turn.cost_usd) : "—"}
                    {" · "}
                    {turn.duration_ms != null ? ms(turn.duration_ms) : "—"}
                  </span>
                  <span className="flag-reason">{row.reasons.join(" · ")}</span>
                  {traceId ? (
                    href ? (
                      <a
                        className="trace-link"
                        href={href}
                        target="_blank"
                        rel="noreferrer noopener"
                        title={`Open trace ${traceId} in Langfuse`}
                        data-testid="fleet-trace-link"
                      >
                        <ExternalLink size={13} aria-hidden /> {traceId.slice(0, 8)}
                      </a>
                    ) : (
                      <span className="trace-none" data-testid="fleet-trace-id" title={`Trace ${traceId}`}>
                        {traceId.slice(0, 8)}
                      </span>
                    )
                  ) : (
                    <span className="trace-none">—</span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
