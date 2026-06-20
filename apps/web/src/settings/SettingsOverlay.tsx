import "./settings.css";

import { Dialog } from "@protolabsai/ui/overlays";
import { Badge } from "@protolabsai/ui/primitives";
import { Server } from "lucide-react";

import { isHostConsole } from "../lib/api";
import { SettingsSurface } from "./SettingsSurface";

// The settings dialog (2026-06 consolidation) — the ONE settings surface (the focused
// agent's settings; the Box group on the host), opened from the utility-bar Settings pill,
// the header drawer, or a ⌘K deep-link. `section` deep-links a section; `key` re-seeds the
// surface per open. (Replaces the rail "Settings" surface + the Global-only overlay.)
export function SettingsOverlay({
  open,
  onClose,
  section,
}: {
  open: boolean;
  onClose: () => void;
  section?: string;
}) {
  if (!open) return null;
  // On the host console these are the box defaults every agent inherits — mark it with a
  // badge in the dialog header (next to "Settings"), not in the body where it pushed the
  // panel content down.
  const title = isHostConsole() ? (
    <span className="settings-overlay-title">
      Settings
      <span
        className="settings-scope-badge"
        title="Box defaults — every agent on this machine inherits these unless it sets its own. Per-agent overrides live under each agent's Settings."
      >
        <Badge status="info"><Server size={12} /> Host · box defaults</Badge>
      </span>
    </span>
  ) : (
    "Settings"
  );
  return (
    <Dialog open onClose={onClose} title={title} width="min(960px, 94vw)" className="settings-overlay">
      <SettingsSurface initialSection={section} key={section || "_"} />
    </Dialog>
  );
}
