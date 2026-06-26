import { Badge } from "@protolabsai/ui/primitives";

import type { PluginUpdate } from "../lib/types";

// Shared freshness indicator (ADR 0027) — joins an installed plugin to its
// update status (GET /api/plugins/updates) and renders a DS Badge next to the
// v{version} ONLY when there's something to say. Used by BOTH the Settings →
// Integrations list (PluginsSection) and the Plugins Local tab (PluginsSurface).
//
//   behind            → "update available" (info)
//   check errored     → "check failed" (warning, error surfaced in the title)
//   up to date        → nothing — the healthy default stays quiet (a badge on
//                       every current plugin is just noise); the Update action
//                       only appears when behind, so "up to date" needs no label
//   pinned (SHA ref)  → nothing — it simply never offers an update
//
// `update` is undefined while the updates query is loading or unavailable (the
// query degrades gracefully) — render nothing then, the row is still complete.
export function PluginFreshness({ update }: { update?: PluginUpdate }) {
  if (!update || update.pinned) return null;
  if (update.error) {
    return (
      <Badge status="warning">
        <span title={`Update check failed: ${update.error}`}>check failed</span>
      </Badge>
    );
  }
  if (update.behind) {
    return (
      <Badge status="info">
        <span title={update.latest_sha ? `latest ${update.latest_sha.slice(0, 10)}` : "a newer commit is available"}>
          update available
        </span>
      </Badge>
    );
  }
  return null; // up to date — no badge
}
