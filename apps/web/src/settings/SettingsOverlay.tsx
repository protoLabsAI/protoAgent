import "./settings.css";

import { Dialog } from "@protolabsai/ui/overlays";
import { Badge } from "@protolabsai/ui/primitives";
import { Server } from "lucide-react";
import { useCallback } from "react";

import { isHostConsole } from "../lib/api";
import { SettingsSurface } from "./SettingsSurface";

/** Topmost-layer-wins Escape arbitration (#2466). The DS Dialog's document-level
 *  Escape listener closes unconditionally, so one keypress dismissed BOTH a nested
 *  dropdown (model picker, any radix menu) and the whole Settings dialog. A radix
 *  layer that just handled this same Escape is still in the DOM during the event
 *  dispatch (React commits after it), so its popper wrapper's presence is exactly
 *  "a nested layer consumed this press". Console-side workaround until the DS
 *  Dialog owns the arbitration — tracked on the DS side; revert this when it does. */
export function escapeCloseAllowed(doc: Document = document): boolean {
  // Interactive layers only (menu/listbox/popover) — a hovered TOOLTIP also rides a
  // popper wrapper but must not hold the dialog open.
  return (
    doc.querySelector(
      '[data-radix-popper-content-wrapper] :is([role="menu"],[role="listbox"],[role="dialog"])',
    ) === null
  );
}

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
  const onCloseGuarded = useCallback(() => {
    if (!escapeCloseAllowed()) return; // the open dropdown consumed this Escape
    onClose();
  }, [onClose]);
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
    <Dialog open onClose={onCloseGuarded} title={title} width="min(960px, 94vw)" className="settings-overlay">
      <SettingsSurface initialSection={section} key={section || "_"} />
    </Dialog>
  );
}
