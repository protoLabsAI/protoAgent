// Fleet group gate (#1708, narrowed in #1999): the header dropdown's "+ New agent" and
// "Fleet settings" share one destination, so they share one gate. Only a spawned workspace
// member driven DIRECTLY is blocked — that's the one window whose /api/fleet is a
// fleet-of-one, where managing would build a nested fleet by accident.
import { describe, expect, it } from "vitest";

import type { FleetAgent } from "../lib/types";
import { FLEET_MEMBER_TOOLTIP, fleetDisabledReason } from "./fleetGate";

const agent = (over: Partial<FleetAgent>): FleetAgent => ({
  name: "a",
  id: "a",
  port: 7871,
  pid: null,
  running: true,
  bundle: "",
  ...over,
});

describe("fleetDisabledReason", () => {
  it("host instance with a fleet → enabled", () => {
    const agents = [agent({ name: "main", id: "main", host: true }), agent({ name: "ava", id: "ava-7f3a" })];
    expect(fleetDisabledReason(agents, "host")).toBeNull();
  });

  it("standalone instance (fleet of one, no member flag) → enabled", () => {
    // A standalone /api/fleet still returns its own host entry — this is the window
    // where the first fleet member gets CREATED, so it must never be locked out.
    const agents = [agent({ name: "main", id: "main", host: true })];
    expect(fleetDisabledReason(agents, "host")).toBeNull();
  });

  it("spawned workspace member reached directly (host entry self-reports member) → disabled", () => {
    const agents = [agent({ name: "ava", id: "ava-7f3a", host: true, member: true })];
    expect(fleetDisabledReason(agents, "host")).toBe(FLEET_MEMBER_TOOLTIP);
  });

  it("member via the hub's slug window → ENABLED (#1999: the roster API stays on the hub)", () => {
    // Reversed from #1708. /api/fleet is an `isHubPath`, so it never rides the slug proxy:
    // creating from this window lands on the hub's fleet, which is what the operator means.
    const agents = [agent({ name: "main", id: "main", host: true }), agent({ name: "ava", id: "ava-7f3a" })];
    expect(fleetDisabledReason(agents, "ava-7f3a")).toBeNull();
    // …and it holds before the fleet poll lands, so the item never flickers disabled→enabled.
    expect(fleetDisabledReason([], "ava-7f3a")).toBeNull();
  });

  it("a slug window is never blocked by the hub's OWN member flag", () => {
    // Guards the ordering inside the gate: the `member` lookup reads the entry flagged
    // `host`, which in a slug window is the hub itself. A hub that is someone else's
    // workspace member (a nested spawn) must not disable the fleet UI for the agents it
    // hosts — only for a console driving that member directly.
    const agents = [agent({ name: "hub", id: "hub", host: true, member: true }), agent({ name: "ava", id: "ava-7f3a" })];
    expect(fleetDisabledReason(agents, "ava-7f3a")).toBeNull();
  });

  it("remote member reached directly at its own URL → enabled (independent instance)", () => {
    // A remote's own /api/fleet host entry never carries `member` — registration is
    // one-sided on the hub, and the remote may legitimately run its OWN fleet.
    const agents = [agent({ name: "peer", id: "peer", host: true })];
    expect(fleetDisabledReason(agents, "host")).toBeNull();
  });
});
