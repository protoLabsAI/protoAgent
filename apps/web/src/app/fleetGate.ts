// Gate for the header dropdown's FLEET GROUP — both "+ New agent" and "Fleet settings"
// (#1708, narrowed in #1999). They share one destination (Global ▸ Fleet), so they share
// one gate: any window that can reach the fleet roster can do both, and any window that
// can't must offer neither.
//
// The fleet CONTROL PLANE is hub-scoped by construction, not by convention: `isHubPath()`
// in lib/api.ts keeps `/api/fleet` + `/api/archetypes` off the slug proxy, so create /
// start / stop / remove always land on the hub no matter which agent the window is focused
// on. That's what makes the matrix below narrow.
//
//   host window (hub, or a standalone instance)
//     → ENABLED. Standalone is where the first member gets created; never lock it out.
//   member via the hub's slug window (/app/agent/<slug>/)
//     → ENABLED. Same origin as the hub, and the roster calls skip the slug proxy, so
//       managing the fleet from here targets the hub correctly. FleetManagerPanel is in
//       fact DESIGNED for this window: its "add as delegate" flow is deliberately
//       slug-scoped, wiring another agent as a `delegate_to` target of the FOCUSED agent
//       (ADR 0042 + 0025) — a thing that only means anything when an agent is focused.
//       (Until #1999 this was disabled on circular grounds: the Box ▸ Fleet group is
//       hidden off-host, so the link "would land nowhere" — but the group was hidden only
//       because `isHostConsole()` is `currentSlug() === "host"`. ADR 0047 §7.7 makes
//       box-shared CONFIG DEFAULTS host-only; a fleet roster is a control plane, not a
//       default, and got lumped in by proximity.)
//   spawned workspace member reached DIRECTLY (its own port)
//     → DISABLED, and genuinely so. Its own /api/fleet is a fleet-of-one (its workspaces
//       root is empty by construction), so "managing" it here builds a NESTED fleet by
//       accident. There is also no hub URL to redirect to: registration is one-sided, so
//       a member holds no back-pointer. A tooltip is the whole remedy available.
//       Signal: the member self-reports `member: true` on its /api/fleet host entry.
//   remote member (ADR 0042 §I) reached directly at its own URL
//     → ENABLED, deliberately. Registration is one-sided on the hub — the remote has no
//       signal it was registered anywhere, and it is a full independent instance that may
//       legitimately run its OWN fleet.

import type { FleetAgent } from "../lib/types";

/** Tooltip copy for the disabled items (also the e2e/unit hook). Worded to fit BOTH
 *  "+ New agent" and "Fleet settings", since one gate now covers the pair. */
export const FLEET_MEMBER_TOOLTIP = "Fleet is managed from the host instance";

/**
 * Why the fleet group is unavailable in this window — or `null` when it's allowed.
 * `agents` is the polled /api/fleet list; `slug` is `currentSlug()` (the window's URL slug,
 * `"host"` when the console talks to its instance directly).
 *
 * Only ONE condition disables: this instance is itself a spawned workspace member being
 * driven directly, where the fleet API underneath us is a fleet-of-one. A hub slug window
 * is NOT blocked — its roster calls already route to the hub.
 */
export function fleetDisabledReason(agents: FleetAgent[], slug: string): string | null {
  // A slug window is proxied BY the hub, so the hub is reachable and the roster calls
  // bypass the proxy — nothing to gate. `slug` is otherwise unused; kept in the signature
  // because the window's identity is the natural thing to ask this question with.
  if (slug !== "host") return null;
  const self = agents.find((a) => a.host);
  if (self?.member) return FLEET_MEMBER_TOOLTIP; // spawned member, reached directly
  return null; // fleet host, or a standalone instance (no fleet configured yet)
}
