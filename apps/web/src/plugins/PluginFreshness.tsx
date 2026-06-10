import { Badge } from "@protolabsai/ui/primitives";

import type { PluginUpdate } from "../lib/types";

// Shared freshness indicator (ADR 0027) — joins an installed plugin to its
// update status (GET /api/plugins/updates) and renders a single DS Badge next to
// the v{version}. Used by BOTH the Settings → Integrations list (PluginsSection)
// and the Plugins rail Local tab (PluginsSurface) so the copy/tone stay identical.
//
//   not behind        → "up to date"  (success)
//   behind            → "update available" (info)
//   pinned (SHA ref)  → "pinned"       (neutral, muted — never auto-updates)
//   check errored     → "check failed" (warning, error surfaced in the title)
//
// `update` is undefined while the updates query is loading or unavailable (the
// query degrades gracefully) — render nothing then, the row is still complete.
export function PluginFreshness({ update }: { update?: PluginUpdate }) {
  if (!update) return null;
  if (update.pinned) {
    return (
      <Badge status="neutral">
        <span title="Pinned to a commit SHA — won't auto-update">pinned</span>
      </Badge>
    );
  }
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
  return <Badge status="success">up to date</Badge>;
}
