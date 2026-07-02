import { Dialog } from "@protolabsai/ui/overlays";
import { useQueryClient } from "@tanstack/react-query";
import { Suspense, useEffect } from "react";

import { queryKeys } from "../lib/queries";
import type { SettingsGroup } from "../lib/types";
import { SettingsCategory } from "../settings/SettingsCategory";
import { pluginSchemaNeedsRefetch } from "./settingsHydration";

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
  const qc = useQueryClient();
  // Fresh-install hydration (#1643): if the cached settings schema predates this
  // plugin's install it has no group for it and the form renders empty until a page
  // refresh. On open, when the group is missing from the cache, invalidate the schema
  // so the mounted query refetches and the fields hydrate in place. (Install paths
  // invalidate it too — usePluginRefresh — this covers every other way in.)
  useEffect(() => {
    if (!open) return;
    const cached = qc.getQueryData<{ groups: SettingsGroup[] }>(queryKeys.settings);
    if (pluginSchemaNeedsRefetch(cached, pluginId)) {
      void qc.invalidateQueries({ queryKey: queryKeys.settings });
    }
  }, [open, pluginId, qc]);

  if (!open) return null;
  return (
    <Dialog open onClose={onClose} title={pluginName} width="min(640px, 95vw)" className="plugin-settings-dialog">
      <Suspense fallback={<p className="muted">Loading settings…</p>}>
        <SettingsCategory category="Plugins" pluginId={pluginId} title="Configuration" />
      </Suspense>
    </Dialog>
  );
}
