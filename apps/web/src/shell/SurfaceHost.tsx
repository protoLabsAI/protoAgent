import { useEffect, useRef, useState, type ReactNode } from "react";
import { Maximize2, PanelRight } from "lucide-react";
import { Button } from "@protolabsai/ui/primitives";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { Dialog, Tooltip } from "@protolabsai/ui/overlays";

import { useUI } from "../state/uiStore";

/** Identity of the view a host frames. The shell resolves the view's content itself and
 *  passes it as `children`, so a plugin view is framed exactly like a core one. */
export type SurfaceMeta = { id: string; title: string };

/**
 * Frame one rail view — in the side rail (`frame="rail"`, wrapping the right dock's own
 * render) or in a large modal (`frame="modal"`, mounted at the shell level while
 * `surfaceModal` is set). Expand moves the rail view into the modal; Dock to rail sends it
 * back and re-selects it in the right dock. The shell renders the SAME view's content
 * through both frames — core surfaces via `renderSurface`, plugin views via the plugin
 * path — so every right-dock view gets the two moves for free, with no per-view wiring.
 *
 * App mounting/registration lives in the shell (App.tsx): the phone mounts neither host
 * (its surfaces live in the mobile shell), so it shows no Expand / Dock control.
 */
export function SurfaceHost({
  frame,
  view,
  children,
}: {
  frame: "rail" | "modal";
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
  // says "docked to the side rail" when a dock clears the modal back onto this very view.
  // A plain Close leaves the rail selection untouched (the cleared modal names some other
  // view), so the rail host stays silent.
  useEffect(() => {
    const was = prevModal.current;
    prevModal.current = modal;
    if (!viewId || !title) return;
    const focusView = () => {
      const target = contentRef.current?.querySelector<HTMLElement>("[data-surface-focus]");
      (target ?? headingRef.current)?.focus();
    };
    if (frame === "modal" && modal?.viewId === viewId) {
      // Parent effects run after the Dialog's own focus trap has initialized.
      focusView();
      setAnnouncement(`${title} opened in a large window.`);
    } else if (frame === "rail" && was && !modal && was.viewId === viewId) {
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
  const body = (
    <div ref={contentRef} style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
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

  if (frame === "modal") {
    if (!view) return null;
    return (
      <>
        {status}
        <Dialog
          open
          height="fill"
          padding="none"
          onClose={() => useUI.getState().closeSurfaceModal()}
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
