// Gate for the fleet affordances a console window offers: the header dropdown's "New agent"
// + "Fleet settings" items, and the palette's Fleet Room. Originally hub-only (#1708/#1999) on the
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

import { ApiError } from "../lib/api";
import type { FleetAgent, FleetStatus } from "../lib/types";

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

// ── Switcher-groups API capability + write-error copy (docs/design/fleet-switcher-groups.md) ──
// The groups/pins write API (#4731 — create/rename/reorder/delete a group, set an agent's group,
// pin/unpin) is OPTIONAL by design: every switcher field on /api/fleet is optional, so a hub that
// predates it returns the flat roster with NO `groups` key at all (spec, Data). On such a hub the
// switcher hides its write controls (star, Pin, Move to group) and renders the plain Ungrouped +
// Remote list, and the Fleet settings group editor shows an update note instead of the editor —
// because POST/PUT/PATCH on the groups/pin routes 404/405 there and would otherwise fail silently
// (this card). A groups-aware hub with no groups yet sends `groups: []`, which reads as supported.

/** True when this hub serves the switcher-groups write API — /api/fleet carried a `groups` array
 *  (even empty). A hub that predates the API omits the field entirely, so `groups` is `undefined`. */
export function fleetHasGroupsApi(fleet: Pick<FleetStatus, "groups"> | undefined): boolean {
  return Array.isArray(fleet?.groups);
}

/** The note shown in Fleet settings ▸ Groups when the hub predates the groups API (verbatim copy). */
export const FLEET_GROUPS_UNSUPPORTED_NOTE =
  "Groups need a newer protoAgent on this machine. Update protoAgent, then come back here to set up groups.";

/** The generic inline-error / toast copy for a rejected group or pin write with no usable hub
 *  message (verbatim copy). */
export const FLEET_WRITE_ERROR = "Couldn't save that change. Try again.";

/** What to tell the operator when a group or pin write is rejected — a group/pin write must never
 *  roll its optimistic change back in silence. A NAME the hub rejected comes back as a 4xx whose
 *  message is already the spec's copy (blank / reserved / duplicate — the server owns the
 *  authoritative check and 400/409/422s with that plain copy); surface it verbatim. Anything else —
 *  a legacy hub's 404/405, a 5xx, a dropped connection — gets the generic copy, never a raw
 *  "Method Not Allowed". Shared by the switcher and the settings editor so both read identically. */
export function fleetWriteErrorMessage(e: unknown): string {
  if (e instanceof ApiError && e.message && (e.status === 400 || e.status === 409 || e.status === 422)) {
    return e.message;
  }
  return FLEET_WRITE_ERROR;
}
