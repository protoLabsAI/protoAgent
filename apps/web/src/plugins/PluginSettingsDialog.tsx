import { Alert } from "@protolabsai/ui/data";
import { Button } from "@protolabsai/ui/primitives";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { Suspense, useEffect } from "react";

import { escapeCloseAllowed, isTopmostOverlay } from "../lib/overlayStack";
import { queryKeys } from "../lib/queries";
import type { PluginSettingsTabDescriptor, SettingsGroup } from "../lib/types";
import { PluginView } from "../app/PluginView";
import { SettingsCategory } from "../settings/SettingsCategory";
import { pluginSchemaNeedsRefetch } from "./settingsHydration";

// Per-plugin settings PANE (settings-ux C1, #4488). ADR 0059 moved the form from an inline
// row into a modal; this supersedes the CONTAINER only — the modal becomes a pane inside the
// Settings stage (Settings ▸ Plugins ▸ <plugin>), following ADR 0048's domain-first Settings IA
// (plugins are a Settings domain, not an overlay stacked on it). A plugin with many tabs
// (projectBoard has five) now gets the full pane width instead of a 760px box over another box
// with two close buttons. The BODY is unchanged from the dialog — the needs-config Alert, the
// same `SettingsCategory` (pluginId-scoped) with `pluginTabs`/`renderPluginTab`, and the
// embedded `PluginView` for path-backed tabs — so save / validate / restart behaviour does not
// change in this slice (C2 changes the save model). A back link + Escape return to the plugin
// list without closing Settings.
export function PluginSettingsPane({
  pluginId,
  pluginName,
  needsConfig,
  settingsTabs,
  pluginLoaded,
  pluginError,
  onBack,
}: {
  pluginId: string;
  pluginName: string;
  // Required-config gate (#1719) — when the plugin is INCOMPLETE, the fields it's
  // missing; renders a setup hint above the form so the pane reads as a wizard.
  // Saving those fields reloads the plugin, which clears `incomplete` and the badge.
  needsConfig?: { key: string; label: string }[];
  settingsTabs?: PluginSettingsTabDescriptor[];
  pluginLoaded?: boolean;
  pluginError?: string;
  // Return to the plugin list (Settings ▸ Plugins) — the back link and Escape both call it.
  onBack: () => void;
}) {
  const qc = useQueryClient();
  // Fresh-install hydration (#1643): if the cached settings schema predates this
  // plugin's install it has no group for it and the form renders empty until a page
  // refresh. On mount, when the group is missing from the cache, invalidate the schema
  // so the mounted query refetches and the fields hydrate in place. (Install paths
  // invalidate it too — usePluginRefresh — this covers every other way in.)
  useEffect(() => {
    const cached = qc.getQueryData<{ groups: SettingsGroup[] }>(queryKeys.settings);
    if (pluginSchemaNeedsRefetch(cached, pluginId)) {
      void qc.invalidateQueries({ queryKey: queryKeys.settings });
    }
  }, [pluginId, qc]);

  // Escape backs out to the plugin list — and must NOT also close Settings on the same press.
  // The pane is a sub-view inside the Settings DS Dialog, whose own Escape→close listener lives
  // on the document (radix). We arbitrate on WINDOW capture — the first stop of the dispatch,
  // before any document listener — with the SAME #2466 checks SettingsOverlay applies (see
  // lib/overlayStack), so a press only backs out the pane when it genuinely belongs to it:
  //   • a DropdownSelect/Combobox open in the SettingsCategory form owns the Escape — defer so
  //     radix dismisses it (escapeCloseAllowed). Crucially we DON'T stop the event in that case,
  //     or the dropdown's own document listener never fires and it stays open;
  //   • a DS dialog stacked ABOVE Settings owns it — defer so that top layer closes
  //     (isTopmostOverlay);
  //   • otherwise the pane is the top layer: stop the press so the Settings dialog's radix
  //     listener never fires (Settings keeps its state) and back out to the plugin list.
  // Guarding the stop also keeps us from ever leaving SettingsOverlay's one-shot `escapeConsumed`
  // flag set for a press we swallowed — that flag only arms when a nested layer is open, which is
  // exactly the case we now defer on (so the next mouse close of Settings can't be eaten).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (!escapeCloseAllowed() || !isTopmostOverlay(".settings-overlay")) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      onBack();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onBack]);

  return (
    // display:contents (see plugins.css) so the header + scrolling body land as the two direct
    // grid rows the `.stage-panel` scaffold expects — same trick as WorkPanel's back-out view.
    <div className="plugin-settings-pane">
      <PanelHeader
        title={
          <span className="plugin-settings-crumbs">
            {/* The "Plugins" crumb IS the back link — Settings ▸ Plugins ▸ <plugin>. */}
            <Button
              variant="ghost"
              size="sm"
              type="button"
              className="plugin-settings-back"
              onClick={onBack}
              data-testid="plugin-config-back"
            >
              <ArrowLeft size={15} /> Plugins
            </Button>
            <span className="plugin-settings-crumb-sep" aria-hidden>›</span>
            <span className="plugin-settings-crumb-current">{pluginName}</span>
          </span>
        }
      />
      {/* The scroll row (grid 1fr). SettingsCategory supplies its own full-width header + padded
          body, so this wrapper adds no padding of its own — only the setup banner is inset. */}
      <div className="plugin-settings-pane-body">
        {needsConfig && needsConfig.length ? (
          <div className="plugin-settings-pane-banner">
            <Alert status="warning" className="settings-banner">
              <strong>Finish setup to activate this plugin.</strong>{" "}
              {`Add the required config below — ${needsConfig.map((n) => n.label).join(", ")}. `}
              Its tools return a “needs setup” notice until then; you can go back and complete it later.
            </Alert>
          </div>
        ) : null}
        <Suspense fallback={<p className="muted">Loading settings…</p>}>
          <SettingsCategory
            category="Plugins"
            pluginId={pluginId}
            pluginTabs={settingsTabs}
            title="Configuration"
            renderPluginTab={(tab, { onRestartPending }) => tab.path ? (
              <PluginView
                key={`plugin:${pluginId}:settings:${tab.id}`}
                embedded
                // A path-backed Configure page is always live (settings-ux C2, #4489): it
                // folds any restart-only change it reports into the pane's one banner.
                onRestartPending={onRestartPending}
                view={{
                  id: tab.id,
                  label: tab.label,
                  path: tab.path,
                  key: `plugin:${pluginId}:settings:${tab.id}`,
                  pluginLoaded,
                  pluginError,
                }}
              />
            ) : null}
          />
        </Suspense>
      </div>
    </div>
  );
}
