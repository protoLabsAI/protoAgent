import { useEffect, useRef, useState, type ReactNode } from "react";
import { Maximize2, PanelRight } from "lucide-react";
import { Button } from "@protolabsai/ui/primitives";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { Dialog, Tooltip } from "@protolabsai/ui/overlays";

import { useUI } from "../state/uiStore";
import type { SurfaceState } from "./surfaceState";

export type SurfaceView = {
  id: string;
  title: string;
  render: (state: SurfaceState, setState: (state: SurfaceState) => void) => ReactNode;
};

const EMPTY_STATE: SurfaceState = {};

/** One active registered view. App mounting/registration is deliberately separate. */
export function SurfaceHost({ views }: { views: readonly SurfaceView[] }) {
  const modal = useUI((s) => s.surfaceModal);
  const rightPanel = useUI((s) => s.rightPanel);
  const states = useUI((s) => s.surfaceStates);
  const viewId = modal?.viewId ?? rightPanel;
  const view = views.find((entry) => entry.id === viewId);
  const frame = modal ? "modal" : "rail";
  const contentRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLSpanElement>(null);
  const previous = useRef("rail");
  const closing = useRef(false);
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    const moved = previous.current !== frame;
    previous.current = frame;
    if (!view || !moved) return;
    // Parent effects run after Dialog's focus trap has initialized.
    const target = contentRef.current?.querySelector<HTMLElement>("[data-surface-focus]");
    (target ?? headingRef.current)?.focus();
    if (closing.current) {
      closing.current = false;
      setAnnouncement("");
      return;
    }
    setAnnouncement(frame === "modal"
      ? `${view.title} opened in a large window.`
      : `${view.title} docked to the side rail.`);
  }, [frame, viewId, view]);

  const heading = <span ref={headingRef} tabIndex={-1} role={frame === "modal" ? "heading" : undefined}
    aria-level={frame === "modal" ? 2 : undefined}>{view?.title}</span>;
  const content = view && (
    <div ref={contentRef} style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
      {view.render(modal?.state ?? states[view.id] ?? EMPTY_STATE,
        (state) => useUI.getState().setSurfaceState(view.id, state))}
    </div>
  );
  const dock = view && (
    <Tooltip label="Dock to rail">
      <Button icon aria-label="Dock to rail" onClick={() => useUI.getState().dockSurface(view.id)}>
        <PanelRight aria-hidden="true" />
      </Button>
    </Tooltip>
  );

  return (
    <>
      <div role="status" aria-live="polite" aria-atomic="true"
        style={{ position: "absolute", clipPath: "inset(50%)", overflow: "hidden", width: 0, height: 0 }}>
        {announcement}
      </div>
      {view && (frame === "modal" ? (
        <Dialog open height="fill" padding="none"
          onClose={() => {
            closing.current = true;
            useUI.getState().closeSurfaceModal();
          }}
          title={<span style={{ display: "flex", alignItems: "center", gap: "var(--pl-space-2)" }}>
            {heading}{dock}
          </span>}>
          {content}
        </Dialog>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
          <PanelHeader title={heading} actions={
            <Tooltip label="Expand">
              <Button icon aria-label="Expand" onClick={() => useUI.getState().openSurfaceModal(view.id)}>
                <Maximize2 aria-hidden="true" />
              </Button>
            </Tooltip>
          } />
          {content}
        </div>
      ))}
    </>
  );
}
