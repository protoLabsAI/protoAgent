// Which Box-group settings sections this window may show (#1999).
//
// Split out of SettingsSurface as pure logic so it's testable without mounting the panel
// tree — this decision is what the FleetSwitcher's "+ New agent" deep-link lands on, and
// getting it wrong is invisible: SettingsSurface resolves a section by
// `sections.find(s => s.id === persisted) ?? sections[0]`, so an unavailable deep-link
// SILENTLY renders an unrelated section rather than erroring.
//
// Host-only (ADR 0047 §7.7 — box-shared state lives on the host console):
//   overview, telemetry
// Available in EVERY window:
//   fleet — the roster API is hub-scoped by construction (`isHubPath` in lib/api.ts keeps
//   /api/fleet + /api/archetypes off the slug proxy), so it reads and writes the hub's
//   fleet from a slug window just as it does from the host. FleetManagerPanel is built for
//   that window besides: its "add as delegate" flow is deliberately slug-scoped to the
//   FOCUSED agent (ADR 0042 + 0025). Entry points are gated separately, on the one window
//   where the fleet below really IS a fleet-of-one — see app/fleetGate.ts.

export type BoxSectionId = "overview" | "fleet" | "telemetry";

/** Box sections for this window, in the order operators know. `onHost` is `isHostConsole()`. */
export function boxSectionIds(onHost: boolean): BoxSectionId[] {
  return onHost ? ["overview", "fleet", "telemetry"] : ["fleet"];
}
