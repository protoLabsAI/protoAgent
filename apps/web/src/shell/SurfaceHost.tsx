import { useEffect, useRef, useState, type ReactNode } from "react";
import { PanelRight } from "lucide-react";
import { Button } from "@protolabsai/ui/primitives";
import { Dialog, Drawer, Tooltip } from "@protolabsai/ui/overlays";

import { openContextMenu, useContextMenuStore } from "../contextMenu/store";
import { useUI } from "../state/uiStore";

/** Identity of the view a host frames. The shell resolves the view's content itself and
 *  passes it as `children`, so a plugin view is framed exactly like a core one. */
export type SurfaceMeta = { id: string; title: string };

// Shared by the rail and modal host instances (one module, one rail + one modal host in the
// shell): the modal host flips this on a plain Close so the rail host's dock branch skips that
// single `surfaceModal`→null transition. Dock to rail and a plain Close both clear the modal the
// SAME way, leaving `rightPanel` unchanged either way — so store state alone can't tell them
// apart. This ref-shaped flag carries the one bit that can, with no store field. A fresh modal open clears it (see the effect).
const pendingClose = { current: false };

// The view background's menu (right-click / Shift+F10 / ContextMenu) held only "Open in window",
// which is off for now — see the note at the top of contextMenu/registrations.tsx. With nothing
// to offer, the rail frame neither opens a menu nor advertises one.
const VIEW_MENU_ENABLED = false;

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
  const modalViewId = modal?.viewId;
  const contentRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLSpanElement>(null);
  const prevModal = useRef(modal);
  // The element that opened a modal or sheet: read during this host's FIRST render, before the
  // Dialog/Drawer mounts and its focus trap moves focus inside. A plain Close gives focus back
  // to it (the dialog pattern); without this the unmount dropped focus to <body>.
  const opener = useRef<Element | null | undefined>(undefined);
  if (opener.current === undefined && frame !== "rail") opener.current = document.activeElement;
  const closedPlainly = useRef(false);
  useEffect(() => () => {
    const target = opener.current;
    if (!closedPlainly.current || !(target instanceof HTMLElement) || !target.isConnected) return;
    // After the overlay finishes unmounting; only if nothing else claimed focus meanwhile.
    requestAnimationFrame(() => {
      const active = document.activeElement;
      if (target.isConnected && (!active || active === document.body)) target.focus({ preventScroll: true });
    });
  }, []);
  const [announcement, setAnnouncement] = useState("");
  const viewId = view?.id;
  const title = view?.title;

  // Move focus into the view as it changes frame, and announce the move for a screen
  // reader. The modal host (a fresh mount) says "opened in a window"; the rail host
  // says "docked to the side rail" when Dock clears the modal back onto this very view.
  // A plain Close clears the modal the same way, so the rail host stays silent only because
  // the modal host flags it via `pendingClose` (see below) — not from store state.
  useEffect(() => {
    const was = prevModal.current;
    prevModal.current = modal;
    if (!viewId || !title) return;
    const focusView = () => {
      // A selected item's heading takes precedence over its (possibly hidden) list row.
      const target = contentRef.current?.querySelector<HTMLElement>('[data-surface-focus][tabindex="-1"]')
        ?? contentRef.current?.querySelector<HTMLElement>("[data-surface-focus]");
      (target ?? headingRef.current ?? contentRef.current)?.focus({ preventScroll: true });
    };
    if ((frame === "modal" || frame === "sheet") && modal?.viewId === viewId) {
      // A fresh open supersedes any pending Close, so clear the shared guard here — this also
      // keeps the rail branch from inheriting a stale flag across opens.
      pendingClose.current = false;
      // Parent effects run after the Dialog's own focus trap has initialized.
      focusView();
      setAnnouncement(`${title} opened in ${frame === "sheet" ? "a full-screen sheet" : "a window"}.`);
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
  // The modal's saved state changes on every scroll. Only a frame/view transition
  // should restore focus; reacting to that state traps keyboard users on the first row.
  }, [frame, modalViewId, viewId, title]);

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
  const canExpand = VIEW_MENU_ENABLED && frame === "rail" && !!view && view.id !== "chat";
  const body = (
    <div
      ref={contentRef}
      role={canExpand ? "region" : undefined}
      aria-label={canExpand ? `${title} view` : undefined}
      aria-haspopup={canExpand ? "menu" : undefined}
      tabIndex={canExpand ? 0 : -1}
      onContextMenu={(e) => {
        // Blank space in full-size child panels is also view background. Let child
        // menus handle the event first, and preserve native menus on controls/editors.
        // Events inside an iframe never bubble here; its rail menu remains available.
        if (!canExpand || e.defaultPrevented || window.getSelection()?.toString()) return;
        if (!(e.target instanceof Element) || !e.currentTarget.contains(e.target)) return;
        const control = e.target.closest(
          'input, textarea, select, button, a[href], iframe, audio, video, summary, [contenteditable]:not([contenteditable="false"]), [role="button"], [role="link"], [role="textbox"], [tabindex]',
        );
        if (control && control !== e.currentTarget && e.currentTarget.contains(control)) return;
        openContextMenu("surface-view", e, { id: view.id });
      }}
      onKeyDown={(e) => {
        if (!canExpand || e.defaultPrevented || e.target !== e.currentTarget) return;
        if (e.key !== "ContextMenu" && !(e.shiftKey && e.key === "F10")) return;
        e.preventDefault();
        const rect = e.currentTarget.getBoundingClientRect();
        useContextMenuStore.getState().openMenu("surface-view", rect.left, rect.top, { id: view.id });
      }}
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
            closedPlainly.current = true;
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
          className="surface-modal"
          onClose={() => {
            // A plain Close (not Dock) — flag it so the rail host stays silent and doesn't pull
            // focus into the rail view on this transition; focus returns to the opener instead.
            pendingClose.current = true;
            closedPlainly.current = true;
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
      {body}
    </div>
  );
}
