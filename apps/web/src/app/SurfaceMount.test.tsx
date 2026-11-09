// Mounting SurfaceHost into the shell (#4955 §3.1, Q16): every right-dock view — core or
// plugin — gets Expand / Dock to rail with no per-view wiring. This exercises the exact
// shape App.tsx mounts: ONE rail host wrapping the right dock's render, mounted whatever the
// active view (the pinned chat view passes no `view`, so it's framed with no Expand), plus a
// shell-level modal host while `surfaceModal` is set, both resolving the SAME view id. The
// phone runs a separate shell — the DS AppShell renders only leftContent below its
// breakpoint — that mounts neither host, so it shows no Expand / Dock control.
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SurfaceHost, type SurfaceMeta } from "../shell/SurfaceHost";
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

// The view's content the shell supplies as children — a marker here; App supplies the real
// renderSurface / plugin-view output through the same path.
function content(id: string) {
  return (
    <>
      <input aria-label="Focus" data-surface-focus />
      <span data-testid={`content-${id}`}>{id}</span>
    </>
  );
}

// Model renderDock: the dock ALWAYS keeps its background-delivery slot mounted (a hidden,
// display-swapped child — the #4802 invariant) and renders the active surface's content
// alongside it. Chat renders elsewhere (the pinned ChatSlot), so the dock shows no content
// for it — only the persistent slot.
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
// SAME view id. The phone shell mounts neither host — its surfaces render bare.
function Shell({ mobile = false }: { mobile?: boolean }) {
  const surfaceModal = useUI((s) => s.surfaceModal);
  const active = useUI((s) => s.rightPanel);
  if (mobile) return <>{META[active] ? content(active) : null}</>;
  return (
    <>
      <SurfaceHost frame="rail" view={active === "chat" ? undefined : META[active]}>
        {dock(active)}
      </SurfaceHost>
      {surfaceModal ? (
        <SurfaceHost frame="modal" view={META[surfaceModal.viewId]}>{content(surfaceModal.viewId)}</SurfaceHost>
      ) : null}
    </>
  );
}

let container: HTMLDivElement;
let root: Root;

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

beforeEach(() => {
  useUI.setState({ surfaceModal: null, surfaceStates: {}, rightPanel: "work", rightCollapsed: false });
  slot.mounts = 0;
  slot.unmounts = 0;
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

    // r1: the view shows an Expand button in its rail header, with no modal open yet.
    expect(button("Expand")).toBeDefined();
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    // r2: Expand opens the SAME view in the shell modal.
    click("Expand");
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(useUI.getState().surfaceModal?.viewId).toBe(id);
    expect(dialog!.querySelector(`[data-testid="content-${id}"]`)).not.toBeNull();

    // r2: Dock to rail closes the modal and makes that view the active right panel.
    click("Dock to rail");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(useUI.getState().rightPanel).toBe(id);
    expect(container.querySelector(`[data-testid="content-${id}"]`)).not.toBeNull();
  });

  // The fix for the rejected attempt's remount (#4955 §3.1, re #4802): the rail host is
  // mounted UNCONDITIONALLY, so switching the right rail between the pinned Chat view (no
  // `view`) and any framed view never changes the dock render's tree position — its
  // background-delivery slot is mounted exactly once and never torn down.
  it("keeps the right dock's background slot mounted across a Chat ⇄ view switch", () => {
    act(() => useUI.setState({ rightPanel: "work" }));
    act(() => root.render(<Shell />));
    expect(button("Expand")).toBeDefined(); // a framed view → Expand present
    expect(slot.mounts).toBe(1);

    // → Chat: the host stays mounted but frames no view, so its header / Expand go away…
    act(() => useUI.setState({ rightPanel: "chat" }));
    expect(button("Expand")).toBeUndefined();
    // …and the background slot is never unmounted.
    expect(slot.mounts).toBe(1);
    expect(slot.unmounts).toBe(0);

    // → back to a (different) framed view: still one mount, Expand returns.
    act(() => useUI.setState({ rightPanel: "plugin:stub:main" }));
    expect(button("Expand")).toBeDefined();
    expect(slot.mounts).toBe(1);
    expect(slot.unmounts).toBe(0);
  });

  it("renders no Expand or Dock control on the phone", () => {
    // A set surfaceModal must still yield no controls — the phone shell mounts no host.
    act(() => useUI.getState().openSurfaceModal("work"));
    act(() => root.render(<Shell mobile />));
    expect(button("Expand")).toBeUndefined();
    expect(button("Dock to rail")).toBeUndefined();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(container.querySelector('[data-testid="content-work"]')).not.toBeNull();
  });
});
