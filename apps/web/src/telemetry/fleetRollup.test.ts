import { describe, expect, it } from "vitest";

import { fleetFlagRows, memberBadge, sortFleetMembers } from "./fleetRollup";

import type { FleetFlag, FleetMemberTelemetry } from "../lib/types";

function flag(member: string, model: string): FleetFlag {
  return {
    member,
    reasons: [`${model} cost outlier`],
    evidence: {
      member,
      turn: { task_id: `${member}-1`, model, cost_usd: 0.12, duration_ms: 8100 },
      trace_id: `${member}-trace`,
      trace_url: `https://langfuse.example.com/project/p1/traces/${member}-trace`,
      timestamp: "2026-06-01T05:00:08+00:00",
    },
  };
}

function member(over: Partial<FleetMemberTelemetry> & { label: string }): FleetMemberTelemetry {
  return {
    name: over.label,
    host: false,
    remote: false,
    running: true,
    reachable: true,
    telemetry_enabled: true,
    rollup: { turns: 3, cost_usd: 0.2, success_rate: 0.9, cache_hit_ratio: 0.6 },
    flags: [],
    ...over,
  };
}

// The members map is keyed by ROUTING SLUG; the host is keyed "host" (label "main")
// and a live peer is keyed by its immutable id "protoEngineer-ba4c" (label
// "protoEngineer"). Two entries deliberately have slug != label; `ava` is the
// slug == label control.
const MEMBERS: Record<string, FleetMemberTelemetry> = {
  ava: member({ label: "ava", flags: [flag("ava", "claude-haiku-4-5")] }),
  "protoEngineer-ba4c": member({ label: "protoEngineer", flags: [flag("protoEngineer-ba4c", "claude-sonnet-4-6")] }),
  roxy: member({ label: "roxy", running: false, reachable: false, telemetry_enabled: false, rollup: null }),
  host: member({ label: "main", host: true, flags: [flag("host", "claude-opus-4-8")] }),
};

describe("sortFleetMembers", () => {
  it("orders host first, then reachable, then unreachable — alpha within group", () => {
    const order = sortFleetMembers(MEMBERS).map((m) => m.label);
    // host ("main") first; reachable running ("ava" < "protoEngineer") next; the
    // unreachable member ("roxy") last regardless of its name.
    expect(order).toEqual(["main", "ava", "protoEngineer", "roxy"]);
  });

  it("attaches the map key as the slug", () => {
    const bySlug = Object.fromEntries(sortFleetMembers(MEMBERS).map((m) => [m.label, m.slug]));
    expect(bySlug.main).toBe("host");
    expect(bySlug.protoEngineer).toBe("protoEngineer-ba4c");
  });
});

describe("memberBadge", () => {
  it("running + telemetry on → success/running", () => {
    expect(memberBadge(member({ label: "x" }))).toEqual({ status: "success", label: "running" });
  });

  it("reachable but idle → warning/idle", () => {
    expect(memberBadge(member({ label: "x", running: false }))).toEqual({ status: "warning", label: "idle" });
  });

  it("unreachable → neutral/unreachable (informational, not error)", () => {
    const b = memberBadge(member({ label: "x", reachable: false, running: false, telemetry_enabled: false }));
    expect(b).toEqual({ status: "neutral", label: "unreachable" });
  });

  it("reachable but telemetry off → neutral/telemetry off", () => {
    expect(memberBadge(member({ label: "x", telemetry_enabled: false }))).toEqual({
      status: "neutral",
      label: "telemetry off",
    });
  });
});

describe("fleetFlagRows — slug→label resolution (the round-2 regression guard)", () => {
  const rows = fleetFlagRows(MEMBERS);

  it("resolves the member label from the members map, NOT the flag's slug", () => {
    // The host flag is keyed under slug "host" but its member label is "main".
    const hostRow = rows.find((r) => r.slug === "host");
    expect(hostRow).toBeDefined();
    expect(hostRow!.memberLabel).toBe("main"); // label — the whole point of the fix
    expect(hostRow!.member).toBe("host"); // the underlying flag still carries the slug
    expect(hostRow!.memberLabel).not.toBe(hostRow!.member); // slug != label, proven

    // A peer whose id (slug) differs from its label — the real live shape.
    const peerRow = rows.find((r) => r.slug === "protoEngineer-ba4c");
    expect(peerRow!.memberLabel).toBe("protoEngineer");
    expect(peerRow!.memberLabel).not.toBe(peerRow!.slug);
  });

  it("keeps identical strings intact for the slug == label case (ava)", () => {
    const avaRow = rows.find((r) => r.slug === "ava");
    expect(avaRow!.memberLabel).toBe("ava");
  });

  it("never renders a routing slug as a member label", () => {
    // No flag row should surface a slug — every label came from the members map.
    const slugs = Object.keys(MEMBERS);
    for (const r of rows) {
      if (r.memberLabel !== r.slug) {
        expect(slugs).not.toContain(r.memberLabel);
      }
    }
  });

  it("carries per-flag evidence (trace + timestamp) through", () => {
    const hostRow = rows.find((r) => r.slug === "host")!;
    expect(hostRow.evidence.trace_id).toBe("host-trace");
    expect(hostRow.evidence.timestamp).toBe("2026-06-01T05:00:08+00:00");
  });
});
