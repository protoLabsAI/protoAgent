// Mounting SurfaceHost into the shell (#4955 §3.1, Q16): every right-dock view — core or
// plugin — gets Expand / Dock to rail with no per-view wiring. This models the exact shape
// App.tsx mounts, 1:1: ONE rail host wrapping the right dock's render, mounted whatever the
// active view (the pinned chat view passes no `view`, so it's framed with no Expand), plus a
// shell-level modal host while `surfaceModal` is set, both resolving the SAME view id. Crucially
// it models App's single-mount rule — while a view is expanded the dock withholds it, so the
// modal owns the only mount (#4802) — and counts mounts so a regression to a double mount fails
// here. The phone uses a sheet host with no Expand / Dock control.
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SurfaceHost, type SurfaceMeta } from "../shell/SurfaceHost";
import "../contextMenu/registrations";
import { useUI } from "../state/uiStore";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// A core view and a plugin view (`plugin:<id>:<viewId>`, Q16) — resolved identically.
const META: Record<string, SurfaceMeta> = {
  work: { id: "work", title: "Work" },
  "plugin:stub:main": { id: "plugin:stub:main", title: "Stub Plugin" },
};

// Mount accounting for the right dock's background-delivery slot — the iframe #4802 says must
// never be torn down when the rail switches surfaces. A remount shows up as a second mount.
const slot = { mounts: 0, unmounts: 0 };
function BgSlot() {
  useEffect(() => {
    slot.mounts += 1;
    return () => {
      slot.unmounts += 1;
    };
  }, []);
  return <span data-testid="bg-slot" />;
}

// Live mount count per view id — a view must be mounted at most once at any instant (the
// one-copy invariant #4802). A double mount (dock AND modal) would read as 2.
const live: Record<string, number> = {};
function Content({ id }: { id: string }) {
  useEffect(() => {
    live[id] = (live[id] ?? 0) + 1;
    return () => {
      live[id] = (live[id] ?? 0) - 1;
    };
  }, [id]);
  return (
    <>
      <input aria-label="Focus" data-surface-focus />
      <span data-testid={`content-${id}`}>{id}</span>
    </>
  );
}
// The view's content the shell supplies as children — a marker here; App supplies the real
// renderSurface / plugin-view output through the same path.
function content(id: string) {
  return <Content id={id} />;
}

// Model renderDock: the dock ALWAYS keeps its background-delivery slot mounted (a hidden,
// display-swapped child — the #4802 invariant) and renders the active surface's content
// alongside it. Chat renders elsewhere (the pinned ChatSlot) and an expanded view is withheld
// (App passes "" for it, the modal owns the mount), so the dock shows no content for those —
// only the persistent slot.
function dock(active: string) {
  return (
    <>
      <div key="bg" className="plugin-bg-slot" style={{ display: "none" }}>
        <BgSlot />
      </div>
      {META[active] ? content(active) : null}
    </>
  );
}

// Mirror App's wiring: the desktop shell wraps the dock render in ONE rail host, mounted
// whatever the active view — the pinned chat view passes no `view`, so it's framed with no
// Expand. While `surfaceModal` is set it also mounts a shell-level modal host resolving the
// SAME view id, and withholds that view from the dock render (App passes "" for it) so the
// modal owns the only mount. The phone frames the expanded surface in a sheet.
function Shell({ mobile = false }: { mobile?: boolean }) {
  const surfaceModal = useUI((s) => s.surfaceModal);
  const active = useUI((s) => s.rightPanel);
  const expandedViewId = surfaceModal?.viewId;
  if (mobile) return (
    <>
      {META[active] && active !== expandedViewId ? content(active) : null}
      {surfaceModal ? (
        <SurfaceHost frame="sheet" view={META[surfaceModal.viewId]}>{content(surfaceModal.viewId)}</SurfaceHost>
      ) : null}
    </>
  );
  return (
    <>
      <SurfaceHost frame="rail" view={active === "chat" ? undefined : META[active]}>
        {dock(expandedViewId === active ? "" : active)}
      </SurfaceHost>
      {surfaceModal ? (
        <SurfaceHost frame="modal" view={META[surfaceModal.viewId]}>{content(surfaceModal.viewId)}</SurfaceHost>
      ) : null}
    </>
  );
}

let container: HTMLDivElement;
let root: Root;

// Opens the rail's current view in the window. The "Open in window" menu entry that used to
// do this is off for now (see contextMenu/registrations.tsx); the pills and palette rows reach
// the same store action, so the shell behavior under test is unchanged.
function expand() {
  act(() => useUI.getState().openSurfaceModal(useUI.getState().rightPanel));
}
function button(label: string) {
  return [...document.querySelectorAll("button")].find(
    (el) => el.getAttribute("aria-label") === label || el.textContent === label,
  );
}
function click(label: string) {
  const el = button(label);
  expect(el).toBeDefined();
  act(() => el!.click());
}
function announced() {
  return [...document.querySelectorAll('[role="status"]')].map((n) => n.textContent).join(" ");
}

beforeEach(() => {
  useUI.setState({ surfaceModal: null, surfaceStates: {}, rightPanel: "work", rightCollapsed: false });
  slot.mounts = 0;
  slot.unmounts = 0;
  for (const k of Object.keys(live)) delete live[k];
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("SurfaceHost mounted in the shell", () => {
  it.each(["work", "plugin:stub:main"])("gives %s an Expand that opens the modal and docks back", (id) => {
    act(() => useUI.setState({ rightPanel: id }));
    act(() => root.render(<Shell />));

    // r1: the view offers a background menu without a duplicate header, with no modal open yet, mounted once.
    expect(button("Expand")).toBeUndefined();
    // The view's background menu is off while "Open in window" is (contextMenu/registrations.tsx).
    expect(document.querySelector('[role="region"][aria-haspopup="menu"]')).toBeNull();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(live[id]).toBe(1);

    // r2: Expand opens the SAME view in the shell modal — and it is mounted EXACTLY once (the
    // dock withheld it, the modal owns the mount). A double mount (dock + modal) would read 2.
    expand();
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(useUI.getState().surfaceModal?.viewId).toBe(id);
    expect(dialog!.querySelector(`[data-testid="content-${id}"]`)).not.toBeNull();
    expect(live[id]).toBe(1);
    // DOM-level double-mount guard: exactly one copy exists anywhere (the modal portals to
    // document.body, the dock lives in `container`; a double mount would count 2).
    expect(document.querySelectorAll(`[data-testid="content-${id}"]`)).toHaveLength(1);

    // r2: Dock to rail closes the modal and makes that view the active right panel — still one mount.
    click("Dock to rail");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(useUI.getState().rightPanel).toBe(id);
    expect(container.querySelector(`[data-testid="content-${id}"]`)).not.toBeNull();
    expect(live[id]).toBe(1);
  });

  // The fix for the rejected attempt's remount (#4955 §3.1, re #4802): the rail host is
  // mounted UNCONDITIONALLY, so switching the right rail between the pinned Chat view (no
  // `view`) and any framed view never changes the dock render's tree position — its
  // background-delivery slot is mounted exactly once and never torn down.
  it("keeps the right dock's background slot mounted across a Chat ⇄ view switch", () => {
    act(() => useUI.setState({ rightPanel: "work" }));
    act(() => root.render(<Shell />));
    expect(button("Expand")).toBeUndefined();
    expect(document.querySelector('[role="region"][aria-haspopup="menu"]')).toBeNull(); // view menu off for now (see registrations.tsx)
    expect(slot.mounts).toBe(1);

    // → Chat: the host stays mounted but frames no view, so its view menu goes away…
    act(() => useUI.setState({ rightPanel: "chat" }));
    expect(button("Expand")).toBeUndefined();
    // …and the background slot is never unmounted.
    expect(slot.mounts).toBe(1);
    expect(slot.unmounts).toBe(0);

    // → back to a (different) framed view: still one mount, the view menu returns.
    act(() => useUI.setState({ rightPanel: "plugin:stub:main" }));
    expect(button("Expand")).toBeUndefined();
    expect(document.querySelector('[role="region"][aria-haspopup="menu"]')).toBeNull();
    expect(slot.mounts).toBe(1);
    expect(slot.unmounts).toBe(0);
  });

  it("Dock to rail announces the move", () => {
    act(() => useUI.setState({ rightPanel: "work" }));
    act(() => root.render(<Shell />));
    expand();
    click("Dock to rail");
    expect(announced()).toContain("docked to the side rail");
    expect(live.work).toBe(1); // back in the dock, mounted once
  });

  // Finding regression guard: a plain Close (the Dialog's ✕ / Escape) of an expanded view
  // clears `surfaceModal` the SAME way Dock does, and the expanded view is `rightActive`, so
  // the rail host's dock branch would fire on this identical transition unless the closing
  // guard suppresses it. A plain Close must leave the rail selection untouched and stay silent.
  it("a plain Close of an expanded view stays silent", () => {
    act(() => useUI.setState({ rightPanel: "work" }));
    act(() => root.render(<Shell />));
    expand();
    click("Close");
    expect(useUI.getState().surfaceModal).toBeNull();
    expect(useUI.getState().rightPanel).toBe("work");
    expect(announced()).not.toContain("docked to the side rail");
    expect(live.work).toBe(1); // back in the dock, mounted once
  });

  it("renders no Expand or Dock control on the phone", () => {
    // The phone sheet owns the only mount and has no frame-switching controls.
    act(() => useUI.getState().openSurfaceModal("work"));
    act(() => root.render(<Shell mobile />));
    expect(button("Expand")).toBeUndefined();
    expect(button("Dock to rail")).toBeUndefined();
    expect(document.querySelector(".pl-drawer--full")).not.toBeNull();
    expect(document.querySelector('[data-testid="content-work"]')).not.toBeNull();
    expect(live.work).toBe(1);
  });
});
