import { useEffect, useRef, useState, type ReactNode } from "react";
import { Maximize2, PanelRight } from "lucide-react";
import { Button } from "@protolabsai/ui/primitives";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { Dialog, Drawer, Tooltip } from "@protolabsai/ui/overlays";

import { useUI } from "../state/uiStore";

/** Identity of the view a host frames. The shell resolves the view's content itself and
 *  passes it as `children`, so a plugin view is framed exactly like a core one. */
export type SurfaceMeta = { id: string; title: string };

// Shared by the rail and modal host instances (one module, one rail + one modal host in the
// shell): the modal host flips this on a plain Close so the rail host's dock branch skips that
// single `surfaceModal`→null transition. Dock to rail and a plain Close both clear the modal the
// SAME way — and Expand only ever targets the active right view, so `rightPanel` is unchanged
// either way — so store state alone can't tell them apart. This ref-shaped flag carries the one
// bit that can, with no store field. A fresh modal open clears it (see the effect).
const pendingClose = { current: false };

/**
 * Frame one rail view — in the side rail (`frame="rail"`, wrapping the right dock's own
 * render) or in a large modal (`frame="modal"`, mounted at the shell level while
 * `surfaceModal` is set). Expand moves the rail view into the modal; Dock to rail sends it
 * back and re-selects it in the right dock. The shell renders the SAME view's content
 * through both frames — core surfaces via `renderSurface`, plugin views via the plugin
 * path — so every right-dock view gets the two moves for free, with no per-view wiring.
 *
 * Phones open the same surface state in a full-screen bottom sheet, with no
 * Expand / Dock control. App mounting lives in the shell (App.tsx).
 */
export function SurfaceHost({
  frame,
  view,
  children,
}: {
  frame: "rail" | "modal" | "sheet";
  view: SurfaceMeta | undefined;
  children: ReactNode;
}) {
  const modal = useUI((s) => s.surfaceModal);
  const contentRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLSpanElement>(null);
  const prevModal = useRef(modal);
  const [announcement, setAnnouncement] = useState("");
  const viewId = view?.id;
  const title = view?.title;

  // Move focus into the view as it changes frame, and announce the move for a screen
  // reader. The modal host (a fresh mount) says "opened in a large window"; the rail host
  // says "docked to the side rail" when Dock clears the modal back onto this very view.
  // A plain Close clears the modal the same way, so the rail host stays silent only because
  // the modal host flags it via `pendingClose` (see below) — not from store state.
  useEffect(() => {
    const was = prevModal.current;
    prevModal.current = modal;
    if (!viewId || !title) return;
    const focusView = () => {
      const target = contentRef.current?.querySelector<HTMLElement>("[data-surface-focus]");
      (target ?? headingRef.current)?.focus();
    };
    if ((frame === "modal" || frame === "sheet") && modal?.viewId === viewId) {
      // A fresh open supersedes any pending Close, so clear the shared guard here — this also
      // keeps the rail branch from inheriting a stale flag across opens.
      pendingClose.current = false;
      // Parent effects run after the Dialog's own focus trap has initialized.
      focusView();
      setAnnouncement(`${title} opened in ${frame === "sheet" ? "a full-screen sheet" : "a large window"}.`);
    } else if (frame === "rail" && was && !modal && was.viewId === viewId) {
      // The modal just cleared back onto this very rail view. Dock to rail moves focus into the
      // view and announces the move; a plain Close (the Dialog's ✕ / Escape / backdrop) must stay
      // silent — it clears `surfaceModal` identically, so the modal host flagged it via the
      // shared `pendingClose` guard for exactly this transition. Consume the flag and bail.
      if (pendingClose.current) {
        pendingClose.current = false;
        return;
      }
      focusView();
      setAnnouncement(`${title} docked to the side rail.`);
    }
  }, [frame, modal, viewId, title]);

  const heading = (
    <span
      ref={headingRef}
      tabIndex={-1}
      role={frame === "modal" ? "heading" : undefined}
      aria-level={frame === "modal" ? 2 : undefined}
    >
      {title}
    </span>
  );
  // A flex COLUMN, not a plain block: the dock surfaces this frames (the code pane, a
  // plugin-view iframe) fill their host with `.stage-panel { flex: 1 1 0 }`, exactly as they
  // did when they were a direct child of the DS dock column. A block parent leaves that
  // `flex` inert, so the surface collapsed to content height (a 150px-tall artifact iframe)
  // and swallowed clicks. `overflow: auto` still lets a natural-height view scroll.
  const body = (
    <div
      ref={contentRef}
      style={{ flex: 1, minHeight: 0, overflow: "auto", display: "flex", flexDirection: "column" }}
    >
      {children}
    </div>
  );
  const status = (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      style={{ position: "absolute", clipPath: "inset(50%)", overflow: "hidden", width: 0, height: 0 }}
    >
      {announcement}
    </div>
  );

  if (frame === "sheet") {
    if (!view) return null;
    return (
      <>
        {status}
        <Drawer
          open
          side="bottom"
          fullScreen
          title={view.title}
          onClose={() => {
            pendingClose.current = true;
            useUI.getState().closeSurfaceModal();
          }}
        >
          {body}
        </Drawer>
      </>
    );
  }

  if (frame === "modal") {
    if (!view) return null;
    return (
      <>
        {status}
        <Dialog
          open
          size="wide"
          height="fill"
          padding="none"
          onClose={() => {
            // A plain Close (not Dock) — flag it so the rail host stays silent and doesn't pull
            // focus into the rail view on this transition.
            pendingClose.current = true;
            useUI.getState().closeSurfaceModal();
          }}
          title={
            <span style={{ display: "flex", alignItems: "center", gap: "var(--pl-space-2)" }}>
              {heading}
              <Tooltip label="Dock to rail">
                <Button icon aria-label="Dock to rail" onClick={() => useUI.getState().dockSurface(view.id)}>
                  <PanelRight aria-hidden="true" />
                </Button>
              </Tooltip>
            </span>
          }
        >
          {body}
        </Dialog>
      </>
    );
  }

  // A view-less rail host frames a PINNED surface (chat): it stays mounted so the shell can
  // wrap the right dock's render unconditionally — keeping the framed content at one tree
  // position whatever the active view — but it adds no header (nothing to Expand) and no
  // full-height footprint (its sibling pinned surface owns the space). The outer div is the
  // SAME element either way and `body` keeps its child index, so the content it frames (and
  // any background plugin iframe inside it) never remounts as `view` appears or clears.
  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0, height: view ? "100%" : undefined }}>
      {status}
      {view ? (
        <PanelHeader
          title={heading}
          actions={
            <Tooltip label="Expand">
              <Button icon aria-label="Expand" onClick={() => useUI.getState().openSurfaceModal(view.id)}>
                <Maximize2 aria-hidden="true" />
              </Button>
            </Tooltip>
          }
        />
      ) : null}
      {body}
    </div>
  );
}
