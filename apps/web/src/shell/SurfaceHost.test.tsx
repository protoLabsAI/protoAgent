import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SurfaceHost, type SurfaceMeta } from "./SurfaceHost";
import { formatSurfaceLink, parseSurfaceLink } from "./surfaceState";
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
  it("heads the view and opens the modal from Expand", () => {
    act(() => root.render(<SurfaceHost frame="rail" view={work}>{content("work")}</SurfaceHost>));
    expect(container.querySelector("h2")?.textContent).toBe("Work");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    click("Expand");
    expect(useUI.getState().surfaceModal?.viewId).toBe("work");
  });

  it("renders its content but no Expand control when it has no view", () => {
    act(() => root.render(<SurfaceHost frame="rail" view={undefined}>{content("work")}</SurfaceHost>));
    expect(container.querySelector('[data-testid="content-work"]')).not.toBeNull();
    expect(button("Expand")).toBeUndefined();
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
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Work opened in a large window.");
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
