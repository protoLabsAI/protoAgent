// Gate for the header dropdown's "Fleet settings" item (#1708). The fleet is managed
// from its HOST instance only (ADR 0042) — a member window gets the item DISABLED with
// a tooltip pointing at the host instead of a broken/irrelevant panel.
//
// Host / member / standalone matrix (window = how the console reaches an instance):
//
//   host window (hub, or any instance with no slug in the URL and no member flag)
//     → ENABLED. Covers both a fleet host and a STANDALONE instance — standalone is
//       where you create your first fleet member, so it must never be locked out.
//   member via the hub's slug window (/app/agent/<slug>/)
//     → DISABLED. The Box ▸ Fleet settings group only exists on the host window
//       (ADR 0047 §7.7): opening it here lands on an unrelated fallback section.
//       Signal: the URL slug (client-side, no fetch needed).
//   spawned workspace member reached DIRECTLY (its own port)
//     → DISABLED. Its own /api/fleet is a fleet-of-one (its workspaces root is empty
//       by construction) — "managing" it there creates a nested fleet by accident.
//       Signal: the member self-reports `member: true` on its /api/fleet host entry.
//   remote member (ADR 0042 §I) reached directly at its own URL
//     → ENABLED, deliberately. Registration is one-sided on the hub — the remote has
//       no signal it was registered anywhere, and it is a full independent instance
//       that may legitimately run its OWN fleet.

import type { FleetAgent } from "../lib/types";

/** Tooltip copy for the disabled item (also the e2e/unit hook). */
export const FLEET_SETTINGS_MEMBER_TOOLTIP = "Fleet settings are managed from the host instance";

/**
 * Why "Fleet settings" is unavailable in this window — or `null` when it's allowed.
 * `agents` is the polled /api/fleet list; `slug` is `currentSlug()` (the window's URL slug,
 * `"host"` when the console talks to its instance directly).
 */
export function fleetSettingsDisabledReason(agents: FleetAgent[], slug: string): string | null {
  if (slug !== "host") return FLEET_SETTINGS_MEMBER_TOOLTIP; // hub slug window on a member
  const self = agents.find((a) => a.host);
  if (self?.member) return FLEET_SETTINGS_MEMBER_TOOLTIP; // spawned member, reached directly
  return null; // fleet host, or a standalone instance (no fleet configured yet)
}
