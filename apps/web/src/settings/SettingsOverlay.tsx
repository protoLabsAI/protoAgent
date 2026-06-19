import "./settings.css";

import { Dialog } from "@protolabsai/ui/overlays";

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
  return (
    <Dialog open onClose={onClose} title="Settings" width="min(960px, 94vw)" className="settings-overlay">
      <SettingsSurface initialSection={section} key={section || "_"} />
    </Dialog>
  );
}
