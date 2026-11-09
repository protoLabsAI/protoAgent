import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SurfaceHost, type SurfaceView } from "./SurfaceHost";
import { formatSurfaceLink, parseSurfaceLink, type SurfaceState } from "./surfaceState";
import { useUI } from "../state/uiStore";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
const place: SurfaceState = {
  section: "goals", itemId: "abc", scrollTop: 240, filters: { status: ["open"], mine: true },
};
const updated: SurfaceState = { ...place, itemId: "def", scrollTop: 480 };
const view: SurfaceView = {
  id: "work", title: "Work",
  render: (state, setState) => (
    <>
      <input aria-label="Surface focus" data-surface-focus defaultValue={state.itemId} />
      <output>{JSON.stringify(state)}</output>
      <button onClick={() => setState(updated)}>Update place</button>
    </>
  ),
};

function click(label: string) {
  const button = [...document.querySelectorAll("button")].find(
    (el) => el.getAttribute("aria-label") === label || el.textContent === label,
  );
  expect(button).toBeDefined();
  act(() => button!.click());
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

describe("SurfaceHost with real DS frames", () => {
  it("retains opaque state in both directions, including edits in the modal", () => {
    useUI.getState().setSurfaceState("work", place);
    act(() => root.render(<SurfaceHost views={[view]} />));
    click("Expand");
    expect(document.querySelector('[role="dialog"] output')?.textContent).toBe(JSON.stringify(place));
    expect(document.activeElement).toBe(document.querySelector("[data-surface-focus]"));
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Work opened in a large window.");
    expect(document.querySelector(".pl-dialog--fill .pl-dialog__body--flush")).not.toBeNull();
    click("Update place");
    click("Dock to rail");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(useUI.getState().rightPanel).toBe("work");
    expect(container.querySelector("output")?.textContent).toBe(JSON.stringify(updated));
    expect(document.activeElement).toBe(container.querySelector("[data-surface-focus]"));
    expect(container.querySelector('[role="status"]')?.textContent).toBe("Work docked to the side rail.");
  });

  it("offers both moves for a stateless view and focuses its heading", () => {
    act(() => root.render(<SurfaceHost views={[{ id: "work", title: "Work", render: () => null }]} />));
    click("Expand");
    expect(document.activeElement?.textContent).toBe("Work");
    expect(document.activeElement?.getAttribute("tabindex")).toBe("-1");
    click("Dock to rail");
    expect(container.querySelector("h2")?.contains(document.activeElement)).toBe(true);
  });

  it("keeps independent places across view switches and closes without changing the rail selection", () => {
    useUI.getState().setSurfaceState("work", place);
    useUI.getState().setSurfaceState("other", { section: "overview" });
    act(() => root.render(<SurfaceHost views={[view, { ...view, id: "other", title: "Other" }]} />));
    act(() => useUI.getState().openSurfaceModal("other"));
    click("Close");
    expect(useUI.getState().surfaceModal).toBeNull();
    expect(useUI.getState().rightPanel).toBe("work");
    expect(container.querySelector("output")?.textContent).toBe(JSON.stringify(place));
    act(() => useUI.getState().openSurfaceModal("other"));
    click("Dock to rail");
    expect(useUI.getState().rightPanel).toBe("other");
    expect(useUI.getState().surfaceStates.other).toEqual({ section: "overview" });
  });

  it("does not persist modal or per-view state; fresh hydration keeps the modal closed", async () => {
    useUI.getState().openSurfaceModal("work", place);
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
