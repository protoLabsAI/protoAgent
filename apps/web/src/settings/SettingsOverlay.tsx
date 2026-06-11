import "./settings.css";

import { Dialog } from "@protolabsai/ui/overlays";

import { SettingsSurface } from "./SettingsSurface";

// The one-stop-shop as an overlay (ADR 0048, operator direction 2026-06-11): the
// same two-home SettingsSurface, openable as a dialog from the topbar gear so
// settings are reachable from anywhere without leaving the current surface. It
// shares the persisted settingsScope/settingsSection, so it opens wherever you
// last were (and quick-settings' "Open full settings" can deep-link a section).
export function SettingsOverlay({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <Dialog open onClose={onClose} title="Settings" width="min(960px, 94vw)" className="settings-overlay">
      <SettingsSurface />
    </Dialog>
  );
}
