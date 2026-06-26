import { Dialog } from "@protolabsai/ui/overlays";
import { Suspense } from "react";

import { SettingsCategory } from "../settings/SettingsCategory";

// Per-plugin settings dialog (ADR 0059, supersedes the inline row expander). Configure
// on a plugin row opens this instead of growing the row — the form gets real breathing
// room for large schemas, and the row stays a single compact line. Reuses the same
// `SettingsCategory` (pluginId-scoped) the inline form used, so save / validate / restart
// behavior is unchanged; only the container moved from an inline panel to a modal.
export function PluginSettingsDialog({
  pluginId,
  pluginName,
  open,
  onClose,
}: {
  pluginId: string;
  pluginName: string;
  open: boolean;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <Dialog open onClose={onClose} title={pluginName} width="min(640px, 95vw)" className="plugin-settings-dialog">
      <Suspense fallback={<p className="muted">Loading settings…</p>}>
        <SettingsCategory category="Plugins" pluginId={pluginId} title="Configuration" />
      </Suspense>
    </Dialog>
  );
}
