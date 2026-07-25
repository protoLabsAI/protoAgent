// Gate for the fleet affordances a console window offers: the header dropdown's "New agent"
// + "Fleet settings" items, and the ⌘K Fleet Room. Originally hub-only (#1708/#1999) on the
// theory that any member window manages a fleet-of-one and so could only spawn NESTED agents
// by accident. That holds for exactly one of the four windows below — a spawned member
// reached DIRECTLY on its own port — so the gate asks that question alone, and every sister
// agent gets the same fleet surfaces its hub has.
//
// Host / member / standalone matrix (window = how the console reaches an instance):
//
//   host window (hub, or any instance with no slug in the URL and no member flag)
//     → ENABLED. Covers both a fleet host and a STANDALONE instance — standalone is
//       where you create your first fleet member, so it must never be locked out.
//   member via the hub's slug window (/app/agent/<slug>/)
//     → ENABLED. `/api/fleet` and `/api/archetypes` are HUB paths in the console router
//       (`isHubPath`, lib/api.ts) — never slug-scoped — so every fleet read and write from
//       a sister agent's window already lands on the hub's supervisor: the hub's real
//       roster, the hub's fleet to create into, the hub's members to start/stop. Nothing
//       nests; the sister manages the same fleet its hub does.
//   spawned workspace member reached DIRECTLY (its own port)
//     → DISABLED. The one true nesting path: its own /api/fleet is a fleet-of-one (its
//       workspaces root is empty by construction), so creating there spawns a GRANDCHILD
//       the hub's roster never shows, and breaks the invariant that a member's empty
//       workspaces root keeps `shutdown_all` hub-only (graph/workspaces/manager.py).
//       Signal: the member self-reports `member: true` on its /api/fleet host entry.
//   remote member (ADR 0042 §I) reached directly at its own URL
//     → ENABLED, deliberately. Registration is one-sided on the hub — the remote has
//       no signal it was registered anywhere, and it is a full independent instance
//       that may legitimately run its OWN fleet.

import type { FleetAgent } from "../lib/types";

/** Tooltip copy for the disabled item (also the e2e/unit hook). */
export const FLEET_SETTINGS_MEMBER_TOOLTIP = "Fleet settings are managed from the host instance";

/**
 * Why the fleet surfaces are unavailable in this window — or `null` when they're allowed.
 * `agents` is the polled /api/fleet list. The window's URL slug deliberately does NOT enter
 * into it: a sister agent's slug window drives the hub's fleet, exactly as the host console does.
 */
export function fleetSettingsDisabledReason(agents: FleetAgent[]): string | null {
  const self = agents.find((a) => a.host);
  if (self?.member) return FLEET_SETTINGS_MEMBER_TOOLTIP; // spawned member, reached directly
  return null; // host, sister-agent slug window, standalone, or a remote's own console
}
