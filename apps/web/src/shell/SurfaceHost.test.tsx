import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SurfaceHost, type SurfaceMeta } from "./SurfaceHost";
import { formatSurfaceLink, parseSurfaceLink } from "./surfaceState";
import "../contextMenu/registrations";
import { resolveMenu } from "../contextMenu/registry";
import { useContextMenuStore } from "../contextMenu/store";
import { useUI } from "../state/uiStore";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
const work: SurfaceMeta = { id: "work", title: "Work" };

// The content the shell would supply as children (a core render / the plugin-view path);
// the host never looks inside it beyond moving focus to its `[data-surface-focus]`.
function content(id: string) {
  return (
    <>
      <input aria-label="Focus" data-surface-focus />
      <span data-testid={`content-${id}`}>{id}</span>
    </>
  );
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

beforeEach(() => {
  useContextMenuStore.getState().close();
  useUI.setState(useUI.getInitialState(), true);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("SurfaceHost rail frame", () => {
  it("keeps only the child heading and actions and opens from its background menu", () => {
    act(() => root.render(<SurfaceHost frame="rail" view={work}><h2>Work</h2><button>History</button></SurfaceHost>));
    expect(container.querySelectorAll("h2")).toHaveLength(1);
    expect(button("History")).toBeDefined();
    expect(button("Expand")).toBeUndefined();
    const host = container.querySelector('[role="region"]')!;
    act(() => host.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    const menu = useContextMenuStore.getState();
    expect(menu.type).toBe("surface-view");
    const entry = resolveMenu(menu.type, menu.ctx).find((e) => e.id === "open-large-window")!;
    expect("run" in entry).toBe(true);
    act(() => { if ("run" in entry) entry.run(menu.ctx, { close: menu.close }); });
    expect(useUI.getState().surfaceModal?.viewId).toBe("work");
  });

  it("opens from nested panel background without replacing a child's custom menu", () => {
    act(() => root.render(<SurfaceHost frame="rail" view={work}>
      <div style={{ flex: 1 }}><section data-testid="background" /></div>
      <div onContextMenu={(e) => {
        e.preventDefault();
        useContextMenuStore.getState().openMenu("child-menu", e.clientX, e.clientY);
      }}><span data-testid="custom">Custom content</span></div>
    </SurfaceHost>));
    const backgroundEvent = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    act(() => container.querySelector('[data-testid="background"]')!.dispatchEvent(backgroundEvent));
    expect(backgroundEvent.defaultPrevented).toBe(true);
    expect(useContextMenuStore.getState()).toMatchObject({ open: true, type: "surface-view", ctx: { id: "work" } });
    act(() => container.querySelector('[data-testid="custom"]')!.dispatchEvent(
      new MouseEvent("contextmenu", { bubbles: true, cancelable: true }),
    ));
    expect(useContextMenuStore.getState().type).toBe("child-menu");
  });

  it.each(["ContextMenu", "F10"])("opens the view menu with %s", (key) => {
    act(() => root.render(<SurfaceHost frame="rail" view={work}>{content("work")}</SurfaceHost>));
    const host = container.querySelector<HTMLElement>('[role="region"]')!;
    host.focus();
    expect(document.activeElement).toBe(host);
    act(() => host.dispatchEvent(new KeyboardEvent("keydown", { key, shiftKey: key === "F10", bubbles: true, cancelable: true })));
    expect(useContextMenuStore.getState()).toMatchObject({ open: true, type: "surface-view", ctx: { id: "work" } });
  });

  it("leaves child editing, custom menus, iframe targets and selected text alone", () => {
    act(() => root.render(<SurfaceHost frame="rail" view={work}>
      <input /><textarea /><select /><a href="#"><span>Link</span></a>
      <div role="button"><span>Action</span></div>
      <div contentEditable suppressContentEditableWarning><span>Editable</span></div>
      <button>Action</button><iframe title="Plugin" />
    </SurfaceHost>));
    for (const child of container.querySelectorAll("input, textarea, select, a span, [role=button] span, [contenteditable] span, button, iframe")) {
      const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
      act(() => child.dispatchEvent(event));
      expect(event.defaultPrevented).toBe(false);
      act(() => child.dispatchEvent(new KeyboardEvent("keydown", { key: "ContextMenu", bubbles: true })));
      expect(useContextMenuStore.getState().open).toBe(false);
    }
    const range = document.createRange();
    range.selectNodeContents(container.querySelector("[contenteditable]")!);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    expect(window.getSelection()!.toString()).toBe("Editable");
    act(() => container.querySelector('[role="region"]')!.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    expect(useContextMenuStore.getState().open).toBe(false);
    window.getSelection()!.removeAllRanges();
  });

  it("preserves the plugin DOM and flex sizing as the pinned host gains and loses a view", () => {
    const render = (view: SurfaceMeta | undefined) => act(() => root.render(
      <SurfaceHost frame="rail" view={view}><iframe title="Plugin" style={{ flex: 1 }} /></SurfaceHost>,
    ));
    render(undefined);
    const iframe = container.querySelector("iframe")!;
    const body = iframe.parentElement!;
    expect(body.style.display).toBe("flex");
    expect(body.style.minHeight).toBe("0px");
    expect(body.style.overflow).toBe("auto");
    render({ id: "plugin:notes:main", title: "Notes" });
    expect(container.querySelector("iframe")).toBe(iframe);
    render(undefined);
    expect(container.querySelector("iframe")).toBe(iframe);
    expect(body.parentElement!.style.height).toBe("");
    expect(body.tabIndex).toBe(-1);
  });

  it.each(["work", "plugin:notes:main"])("expands inactive collapsed rail id %s and preserves state through Dock", (id) => {
    const state = { section: "all", itemId: "item-1", scrollTop: 321 };
    useUI.setState({ rightPanel: "knowledge", rightCollapsed: true,
      railOrder: { left: ["chat"], right: ["knowledge", id], bottom: [], hidden: [] },
      surfaceStates: { [id]: state } });
    const entries = resolveMenu("rail-surface", { id, side: "right", pluginId: "notes", pluginUpdatable: true });
    expect(entries.map((e) => e.id)).toEqual(expect.arrayContaining(["move-up", "hide", "configure", "update"]));
    const entry = entries.find((e) => e.id === "open-large-window")!;
    act(() => { if ("run" in entry) entry.run({}, { close: () => {} }); });
    expect(useUI.getState().surfaceModal).toEqual({ viewId: id, state });
    expect(useUI.getState().rightPanel).toBe("knowledge");
    act(() => useUI.getState().dockSurface(id));
    expect(useUI.getState()).toMatchObject({ rightPanel: id, rightCollapsed: false, surfaceStates: { [id]: state } });
  });

  it("excludes Chat and other docks from rail expansion", () => {
    for (const side of ["left", "right", "bottom"] as const) {
      useUI.setState({ railOrder: { left: [], right: [], bottom: [], hidden: [], [side]: ["chat", "work"] } });
      expect(resolveMenu("rail-surface", { id: "chat", side }).some((e) => e.id === "open-large-window")).toBe(false);
      if (side !== "right") expect(resolveMenu("rail-surface", { id: "work", side }).some((e) => e.id === "open-large-window")).toBe(false);
    }
  });

  it("focuses the host without adding a heading when docking content without a focus marker", () => {
    act(() => root.render(<SurfaceHost frame="rail" view={work}><div>Panel</div></SurfaceHost>));
    act(() => useUI.getState().openSurfaceModal("work"));
    act(() => useUI.getState().dockSurface("work"));
    expect(document.activeElement).toBe(container.querySelector('[role="region"]'));
    expect(container.querySelector("h2")).toBeNull();
  });

  it("renders its content but no Expand control when it has no view", () => {
    act(() => root.render(<SurfaceHost frame="rail" view={undefined}>{content("work")}</SurfaceHost>));
    expect(container.querySelector('[data-testid="content-work"]')).not.toBeNull();
    expect(button("Expand")).toBeUndefined();
  });

  it("does not offer a view menu on pinned Chat", () => {
    act(() => root.render(<SurfaceHost frame="rail" view={{ id: "chat", title: "Chat" }}>{content("chat")}</SurfaceHost>));
    const body = container.querySelector('[data-testid="content-chat"]')!.parentElement!;
    expect(body.tabIndex).toBe(-1);
    expect(container.querySelector('[role="region"]')).toBeNull();
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    act(() => body.dispatchEvent(event));
    expect(event.defaultPrevented).toBe(false);
    expect(useContextMenuStore.getState().open).toBe(false);
  });
});

describe("SurfaceHost modal frame", () => {
  it("frames the content in a dialog, focuses it, and announces the move", () => {
    act(() => useUI.getState().openSurfaceModal("work"));
    act(() => root.render(<SurfaceHost frame="modal" view={work}>{content("work")}</SurfaceHost>));
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog!.querySelector('[data-testid="content-work"]')).not.toBeNull();
    // The modal frame passes `size="wide"` (DS ds-zb8), so the DS Dialog rides the
    // `pl-dialog--wide` class — the near-full-screen width, no per-app width override.
    expect(dialog!.classList.contains("pl-dialog--wide")).toBe(true);
    expect(document.querySelector(".pl-dialog--fill .pl-dialog__body--flush")).not.toBeNull();
    expect(document.activeElement).toBe(dialog!.querySelector("[data-surface-focus]"));
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Work opened in a window.");
  });

  it.each(["left", "right", "bottom", "hidden"] as const)(
    "Dock to rail reveals Work from %s and preserves its section",
    (placement) => {
      const railOrder = { left: ["chat"], right: ["knowledge"], bottom: ["logs"], hidden: [] as string[] };
      railOrder[placement].push("work");
      useUI.setState({
        railOrder, surface: "chat", rightPanel: "knowledge", bottomPanel: "logs",
        leftCollapsed: true, rightCollapsed: true, bottomCollapsed: true,
        rightWidth: 0, bottomHeight: 0,
      });
      act(() => useUI.getState().openSurfaceModal("work", { section: "goals" }));
      act(() => root.render(<SurfaceHost frame="modal" view={work}>{content("work")}</SurfaceHost>));
      click("Dock to rail");
      const s = useUI.getState();
      const side = placement === "hidden" ? "right" : placement;
      expect(s.surfaceModal).toBeNull();
      expect(s.railOrder[side]).toContain("work");
      expect(s.railOrder.hidden).not.toContain("work");
      expect(s.surfaceStates.work).toEqual({ section: "goals" });
      expect(s.surface).toBe(side === "left" ? "work" : "chat");
      expect(s.rightPanel).toBe(side === "right" ? "work" : "knowledge");
      expect(s.bottomPanel).toBe(side === "bottom" ? "work" : "logs");
      expect(s[`${side}Collapsed`]).toBe(false);
      if (side === "right") expect(s.rightWidth).toBe(360);
      if (side === "bottom") expect(s.bottomHeight).toBe(240);
    },
  );

  it("Close clears the modal without changing the rail selection", () => {
    useUI.setState({ rightPanel: "work" });
    act(() => useUI.getState().openSurfaceModal("other"));
    act(() => root.render(<SurfaceHost frame="modal" view={{ id: "other", title: "Other" }}>{content("other")}</SurfaceHost>));
    click("Close");
    expect(useUI.getState().surfaceModal).toBeNull();
    expect(useUI.getState().rightPanel).toBe("work");
  });
});

describe("SurfaceHost modal focus return", () => {
  const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

  it.each(["modal", "sheet"] as const)("a plain Close of the %s returns focus to the element that opened it", async (kind) => {
    const opener = document.createElement("button");
    opener.textContent = "Decisions";
    document.body.appendChild(opener);
    opener.focus();
    act(() => useUI.getState().openSurfaceModal("work"));
    act(() => root.render(<SurfaceHost frame={kind} view={work}>{content("work")}</SurfaceHost>));
    expect(document.activeElement).not.toBe(opener); // the overlay took focus
    if (kind === "modal") click("Close");
    else act(() => (document.querySelector(".pl-drawer [aria-label='Close']") as HTMLElement | null)?.click());
    expect(useUI.getState().surfaceModal).toBeNull();
    // The shell unmounts the overlay host once surfaceModal clears.
    act(() => root.render(<></>));
    await act(frame);
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("Dock to rail does not pull focus back to the opener", async () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    act(() => useUI.getState().openSurfaceModal("work"));
    act(() => root.render(<SurfaceHost frame="modal" view={work}>{content("work")}</SurfaceHost>));
    click("Dock to rail");
    act(() => root.render(<></>));
    await act(frame);
    expect(document.activeElement).not.toBe(opener);
    opener.remove();
  });
});

describe("surface modal state is never persisted", () => {
  it("omits surfaceModal / surfaceStates from storage and hydrates the modal closed", async () => {
    act(() => useUI.getState().openSurfaceModal("work", { section: "goals" }));
    const options = useUI.persist.getOptions();
    const saved = await options.storage!.getItem(options.name!);
    expect(saved?.state).not.toHaveProperty("surfaceModal");
    expect(saved?.state).not.toHaveProperty("surfaceStates");
    // Reset without writing storage, then hydrate the exact saved payload.
    useUI.persist.setOptions({ storage: {
      getItem: () => saved,
      setItem: () => {},
      removeItem: () => {},
    } });
    try {
      useUI.setState(useUI.getInitialState(), true);
      await useUI.persist.rehydrate();
      expect(useUI.getState().surfaceModal).toBeNull();
    } finally {
      useUI.persist.setOptions({ storage: options.storage });
    }
  });
});

describe("surface deep links", () => {
  it.each(["work/goals/abc", "work/overview", "plugin%3Anotes%3Amain/all/a%2Fb"])("round-trips %s", (link) => {
    const { viewId, state } = parseSurfaceLink(link);
    expect(formatSurfaceLink(viewId, state)).toBe(link);
  });
  it.each(["", "/goals", "work", "work/", "work/goals/", "work/goals/a/extra", "work/%ZZ"])("rejects malformed link %s", (link) => {
    expect(() => parseSurfaceLink(link)).toThrow();
  });
  it("rejects an empty view id when formatting", () => {
    expect(() => formatSurfaceLink("", { section: "overview" })).toThrow();
  });
});


describe("SurfaceHost sheet frame", () => {
  it("renders a titled full-screen bottom sheet and keeps state across close and reopen", () => {
    useUI.setState({ rightPanel: "knowledge" });
    const state = { section: "goals", itemId: "goal-1", filters: { query: "saved" } };
    act(() => useUI.getState().openSurfaceModal("work", state));
    act(() => root.render(<SurfaceHost frame="sheet" view={work}>{content("work")}</SurfaceHost>));
    const sheet = document.querySelector(".pl-drawer--bottom.pl-drawer--full")!;
    expect(sheet).not.toBeNull();
    expect(sheet.querySelector(".pl-drawer__title")?.textContent).toBe("Work");
    expect(sheet.querySelector('[data-testid="content-work"]')).not.toBeNull();
    expect(document.querySelector(".pl-dialog")).toBeNull();
    expect(button("Expand")).toBeUndefined();
    expect(button("Dock to rail")).toBeUndefined();
    expect(document.activeElement).toBe(sheet.querySelector("[data-surface-focus]"));
    click("Close");
    expect(useUI.getState().surfaceModal).toBeNull();
    expect(useUI.getState().rightPanel).toBe("knowledge");
    expect(useUI.getState().surfaceStates.work).toEqual(state);
    act(() => root.render(null));
    act(() => useUI.getState().openSurfaceModal("work"));
    act(() => root.render(<SurfaceHost frame="sheet" view={work}>{content("work")}</SurfaceHost>));
    expect(useUI.getState().surfaceModal?.state).toEqual(state);
  });
});
