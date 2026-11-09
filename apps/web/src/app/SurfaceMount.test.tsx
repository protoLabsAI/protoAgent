// Mounting SurfaceHost into the shell (#4955 §3.1, Q16): every right-dock view — core or
// plugin — gets Expand / Dock to rail with no per-view wiring. This exercises the exact
// shape App.tsx mounts: a rail host wrapping the right dock's render, plus a shell-level
// modal host while `surfaceModal` is set, both resolving the SAME view id. The phone runs a
// separate shell that mounts neither host, so it shows no Expand / Dock control.
import { act } from "react";
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

// Mirror App's wiring: the desktop shell mounts a rail host (wrapping the active right
// view's render) and, while `surfaceModal` is set, a shell-level modal host. The phone
// shell mounts neither — its surfaces render bare.
function Shell({ mobile = false }: { mobile?: boolean }) {
  const surfaceModal = useUI((s) => s.surfaceModal);
  const rightPanel = useUI((s) => s.rightPanel);
  const active = META[rightPanel] ? rightPanel : "work";
  if (mobile) return content(active);
  return (
    <>
      <SurfaceHost frame="rail" view={META[active]}>{content(active)}</SurfaceHost>
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
