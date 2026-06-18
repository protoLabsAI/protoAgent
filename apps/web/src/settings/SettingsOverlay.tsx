import "./settings.css";

import { Dialog } from "@protolabsai/ui/overlays";

import { SettingsSurface } from "./SettingsSurface";

// Global (box-shared) settings as an overlay, opened from the header drawer
// (2026-06-18 IA pass): the Global home — Overview · Configuration · Fleet ·
// Telemetry · Commons — without the scope toggle. Workspace settings live in the
// rail surface, not here. `section` deep-links a Global section (e.g. the drawer's
// "Telemetry" item opens straight to it); the `key` re-seeds the surface per open.
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
    <Dialog open onClose={onClose} title="Global settings" width="min(960px, 94vw)" className="settings-overlay">
      <SettingsSurface only="host" initialSection={section} key={section || "_"} />
    </Dialog>
  );
}
