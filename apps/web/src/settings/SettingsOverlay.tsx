import "./settings.css";

import { Dialog } from "@protolabsai/ui/overlays";
import { Badge } from "@protolabsai/ui/primitives";
import { Server } from "lucide-react";
import { useCallback, useEffect, useRef } from "react";

import { isHostConsole } from "../lib/api";
import { SettingsSurface } from "./SettingsSurface";

/** Is there an open interactive layer (dropdown/menu/listbox) above the dialog? (#2466)
 *
 *  Interactive layers only — a hovered TOOLTIP also rides a popper wrapper but must not
 *  hold the dialog open.
 *
 *  WHEN you ask this is the whole problem; see `SettingsOverlay` below. */
export function escapeCloseAllowed(doc: Document = document): boolean {
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
  // Topmost-layer-wins Escape arbitration (#2466). The DS Dialog's Escape listener closes
  // unconditionally, so one keypress dismissed BOTH the open dropdown and the whole
  // Settings dialog — losing the operator's section and any unsaved edits.
  //
  // The catch is TIMING, and it's why the first attempt at this didn't work: asking
  // "is a layer open?" from the close handler always answers NO. Radix dismisses its
  // layer from a document-capture keydown listener, and React 18 flushes discrete input
  // events synchronously — so the menu is already unmounted before any other handler in
  // the dispatch observes the event. (Measured: at `document` capture the layer is gone;
  // the predicate can never see it there.)
  //
  // So sample it on WINDOW capture instead. Window is the first node in the capture path,
  // so this runs before every document-level listener — radix's and the Dialog's — no
  // matter what order they registered in. The sample is one-shot: whoever asks first
  // consumes it, so a later mouse-driven close can't be swallowed by a stale reading.
  const escapeConsumed = useRef(false);
  useEffect(() => {
    if (!open) return;
    const sample = (e: KeyboardEvent) => {
      if (e.key === "Escape") escapeConsumed.current = !escapeCloseAllowed();
    };
    window.addEventListener("keydown", sample, true);
    return () => window.removeEventListener("keydown", sample, true);
  }, [open]);

  const onCloseGuarded = useCallback(() => {
    const consumed = escapeConsumed.current;
    escapeConsumed.current = false; // one-shot — never outlives the press that set it
    if (consumed) return; // a nested layer took this Escape; the dialog keeps its state
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
