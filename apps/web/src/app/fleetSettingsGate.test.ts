// "Fleet settings" gate (#1708): the header dropdown item is hub-only — disabled with
// a point-at-the-host tooltip in member windows, enabled on the host AND on a
// standalone instance (standalone is where you create your first member).
import { describe, expect, it } from "vitest";

import type { FleetAgent } from "../lib/types";
import { FLEET_SETTINGS_MEMBER_TOOLTIP, fleetSettingsDisabledReason } from "./fleetSettingsGate";

const agent = (over: Partial<FleetAgent>): FleetAgent => ({
  name: "a",
  id: "a",
  port: 7871,
  pid: null,
  running: true,
  bundle: "",
  ...over,
});

describe("fleetSettingsDisabledReason", () => {
  it("host instance with a fleet → enabled", () => {
    const agents = [agent({ name: "main", id: "main", host: true }), agent({ name: "ava", id: "ava-7f3a" })];
    expect(fleetSettingsDisabledReason(agents, "host")).toBeNull();
  });

  it("standalone instance (fleet of one, no member flag) → enabled", () => {
    // A standalone /api/fleet still returns its own host entry — this is the window
    // where the first fleet member gets CREATED, so it must never be locked out.
    const agents = [agent({ name: "main", id: "main", host: true })];
    expect(fleetSettingsDisabledReason(agents, "host")).toBeNull();
  });

  it("spawned workspace member reached directly (host entry self-reports member) → disabled", () => {
    const agents = [agent({ name: "ava", id: "ava-7f3a", host: true, member: true })];
    expect(fleetSettingsDisabledReason(agents, "host")).toBe(FLEET_SETTINGS_MEMBER_TOOLTIP);
  });

  it("member via the hub's slug window → disabled (slug alone decides)", () => {
    const agents = [agent({ name: "main", id: "main", host: true }), agent({ name: "ava", id: "ava-7f3a" })];
    expect(fleetSettingsDisabledReason(agents, "ava-7f3a")).toBe(FLEET_SETTINGS_MEMBER_TOOLTIP);
    // Even before the fleet poll lands, the slug already marks a member window.
    expect(fleetSettingsDisabledReason([], "ava-7f3a")).toBe(FLEET_SETTINGS_MEMBER_TOOLTIP);
  });

  it("remote member reached directly at its own URL → enabled (independent instance)", () => {
    // A remote's own /api/fleet host entry never carries `member` — registration is
    // one-sided on the hub, and the remote may legitimately run its OWN fleet.
    const agents = [agent({ name: "peer", id: "peer", host: true })];
    expect(fleetSettingsDisabledReason(agents, "host")).toBeNull();
  });
});
