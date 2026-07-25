// The fleet-surface gate (#1708/#1999 → sister agents). "New agent", "Fleet settings" and the
// ⌘K Fleet Room are live in every window that drives a REAL fleet — the host, a standalone
// instance, and a sister agent's slug window (its fleet calls are hub paths, so they manage the
// hub's fleet). Only a spawned member reached DIRECTLY on its own port is held back, since
// there the fleet genuinely is a fleet-of-one and creating would nest.
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
    expect(fleetSettingsDisabledReason(agents)).toBeNull();
  });

  it("standalone instance (fleet of one, no member flag) → enabled", () => {
    // A standalone /api/fleet still returns its own host entry — this is the window
    // where the first fleet member gets CREATED, so it must never be locked out.
    const agents = [agent({ name: "main", id: "main", host: true })];
    expect(fleetSettingsDisabledReason(agents)).toBeNull();
  });

  it("spawned workspace member reached directly (host entry self-reports member) → disabled", () => {
    const agents = [agent({ name: "ava", id: "ava-7f3a", host: true, member: true })];
    expect(fleetSettingsDisabledReason(agents)).toBe(FLEET_SETTINGS_MEMBER_TOOLTIP);
  });

  it("sister agent via the hub's slug window → ENABLED (the roster it sees is the hub's)", () => {
    // The window's slug is deliberately not an input: /api/fleet + /api/archetypes are hub
    // paths (never slug-scoped), so this window creates into — and manages — the hub's fleet.
    // The entry carrying `host` is the HUB's, and a hub never self-reports `member`.
    const agents = [agent({ name: "main", id: "main", host: true }), agent({ name: "ava", id: "ava-7f3a" })];
    expect(fleetSettingsDisabledReason(agents)).toBeNull();
  });

  it("remote member reached directly at its own URL → enabled (independent instance)", () => {
    // A remote's own /api/fleet host entry never carries `member` — registration is
    // one-sided on the hub, and the remote may legitimately run its OWN fleet.
    const agents = [agent({ name: "peer", id: "peer", host: true })];
    expect(fleetSettingsDisabledReason(agents)).toBeNull();
  });

  it("an empty roster (the poll hasn't landed) → enabled, so nothing flickers disabled", () => {
    // Nothing here proves a fleet-of-one; disabled must never be the cold-open default or the
    // menu would flash a point-at-the-host tooltip on every load.
    expect(fleetSettingsDisabledReason([])).toBeNull();
  });
});
