// Pure derivations for the Telemetry surface's Fleet section (ADR 0006 fleet
// extension). Kept out of the component so the ordering, badge, and — critically —
// the slug→label resolution are unit-testable without rendering React.

import type { FleetFlag, FleetMemberTelemetry } from "../lib/types";

export type FleetMemberRow = FleetMemberTelemetry & { slug: string };

// Deterministic fleet-grid order: the host first, then reachable running members,
// then reachable idle members, then unreachable ones — each group alphabetical by
// label. The members map has no inherent order, so the surface (and its e2e) need a
// stable one.
export function sortFleetMembers(
  members: Record<string, FleetMemberTelemetry>,
): FleetMemberRow[] {
  const rank = (m: FleetMemberTelemetry) =>
    m.host ? 0 : !m.reachable ? 3 : m.running ? 1 : 2;
  return Object.entries(members)
    .map(([slug, m]) => ({ ...m, slug }))
    .sort((a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label));
}

export type MemberBadgeStatus = "success" | "warning" | "neutral" | "error";
export type MemberBadge = { status: MemberBadgeStatus; label: string };

// Reachability/running badge. An unreachable member is the INFORMATIONAL state
// (neutral, "unreachable") — it simply didn't answer the rollup fan-out; the hub
// never restarts it. A reachable member with telemetry off is neutral too; running
// vs idle is the success/warning split.
export function memberBadge(m: FleetMemberTelemetry): MemberBadge {
  if (!m.reachable) return { status: "neutral", label: "unreachable" };
  if (!m.telemetry_enabled) return { status: "neutral", label: "telemetry off" };
  return m.running
    ? { status: "success", label: "running" }
    : { status: "warning", label: "idle" };
}

export type FleetFlagRow = FleetFlag & {
  slug: string;
  // The member's DISPLAY label, resolved from the members map — never the raw
  // routing slug (`flag.member`). Deriving it here, by iterating the members map,
  // makes a slug/label mismatch structurally impossible to render: a host keyed
  // "host" with label "main" renders "main", and a peer keyed "protoEngineer-ba4c"
  // with label "protoEngineer" renders "protoEngineer".
  memberLabel: string;
};

// Every member's flagged problems, flattened for the "Flagged problems" list, each
// row carrying its member's resolved label. Host-first, matching sortFleetMembers.
export function fleetFlagRows(
  members: Record<string, FleetMemberTelemetry>,
): FleetFlagRow[] {
  return sortFleetMembers(members).flatMap((m) =>
    m.flags.map((flag) => ({ ...flag, slug: m.slug, memberLabel: m.label })),
  );
}
