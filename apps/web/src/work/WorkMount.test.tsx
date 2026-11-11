// The Work surface mounted into the shell (#4955). App.tsx `renderSurface` case "work" now
// returns <WorkView> (not the retired WorkPanel) inside the SurfaceHost frame — the rail dock
// frames it with Expand, and the shell modal frames the SAME surface state on Expand. This
// models that wiring 1:1 (one rail host, one modal host, both resolving the "work" view) with
// the REAL WorkView and the REAL section registry, so it proves the two things a stub can't:
// every built-in section is registered (none falls back to the unregistered Empty), and the
// view round-trips section state across Expand ⇄ Dock to rail.
import { act } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SurfaceHost, type SurfaceMeta } from "../shell/SurfaceHost";
import { WorkView } from "./WorkView";
import { sections } from "./sectionRegistry";
import "../contextMenu/registrations";
import { useUI } from "../state/uiStore";
import type { WorkItems, WorkSource, WorkSourceKind } from "./useWorkItems";
// Importing the production barrel is the whole point of the mount — it must register all eight
// built-in sections. Do NOT import the individual section modules here, or a missing entry in
// the barrel would be masked by a direct import (the same guard WorkView.test.tsx keeps).
import "./sections";

const data = vi.hoisted(() => ({ sources: {} as WorkItems }));
vi.mock("./useWorkItems", () => ({ useWorkItems: () => data.sources }));
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const KINDS: WorkSourceKind[] = ["decisions", "goals", "watches", "tasks", "schedules", "background", "activity", "directives"];
const WORK: SurfaceMeta = { id: "work", title: "Work" };

let client: QueryClient;
let root: Root;
let host: HTMLDivElement;

// Mirror App.tsx's shell wiring: ONE rail host frames the right dock's WorkView, and while
// `surfaceModal` is set a shell-level modal host frames the SAME view (the dock withholds it so
// the modal owns the single mount, #4802). Both WorkView instances read one `surfaceStates.work`.
function Shell() {
  const surfaceModal = useUI((s) => s.surfaceModal);
  const active = useUI((s) => s.rightPanel);
  const expanded = surfaceModal?.viewId;
  return (
    <>
      <SurfaceHost frame="rail" view={active === "chat" ? undefined : WORK}>
        {expanded === active ? null : <WorkView frame="rail" />}
      </SurfaceHost>
      {surfaceModal ? (
        <SurfaceHost frame="modal" view={WORK}>
          <WorkView frame="modal" />
        </SurfaceHost>
      ) : null}
    </>
  );
}

async function render() {
  await act(async () =>
    root.render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <Shell />
        </ToastProvider>
      </QueryClientProvider>,
    ),
  );
}
// Opens Work in the window. The "Open in window" menu entry is off for now
// (contextMenu/registrations.tsx); the pills and palette rows use this same store action.
function expand() {
  act(() => useUI.getState().openSurfaceModal("work"));
}
function button(label: string) {
  return [...document.querySelectorAll("button")].find(
    (el) => el.getAttribute("aria-label") === label || el.textContent?.trim() === label,
  );
}
async function click(label: string) {
  const el = button(label);
  expect(el, label).toBeDefined();
  await act(async () => el!.click());
}
function activeSection() {
  return document.querySelector(".work-view__picker button")?.textContent ?? "";
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  data.sources = Object.fromEntries(
    KINDS.map((kind) => [kind, { items: [], status: "ready", error: null, loadedAt: 1, retry: vi.fn() } as WorkSource]),
  ) as WorkItems;
  useUI.setState({ surfaceModal: null, surfaceStates: {}, rightPanel: "work", rightCollapsed: false });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

describe("Work surface mount", () => {
  it("registers all eight built-in sections through the barrel import", () => {
    for (const kind of KINDS) expect(sections.has(kind), kind).toBe(true);
  });

  it("frames WorkView — not WorkPanel — in the right dock, with the Overview and nine section menu items", async () => {
    await render();
    // It's WorkView: its two-pane shell and section nav, never a WorkPanel overview card.
    expect(host.querySelector(".work-view__panes")).not.toBeNull();
    expect(host.querySelector('[data-testid^="work-card-"]')).toBeNull();
    // Overview plus the eight sources = nine entries in the nav.
    const trigger = host.querySelector<HTMLButtonElement>(".work-view__picker button")!;
    await act(async () => trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    const menu = document.querySelector<HTMLElement>('[role="menu"][aria-label="Work sections"]')!;
    expect(menu.querySelectorAll('[role="menuitemradio"]')).toHaveLength(9);
    expect(menu.querySelector('[aria-checked="true"]')?.textContent).toContain("Overview");
    await act(async () => menu.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(host.textContent).toContain("Nothing going on");
    // Framed by the rail host; its view menu is off for now (see registrations.tsx).
    expect(button("Expand")).toBeUndefined();
    expect(document.querySelector('[role="region"][aria-haspopup="menu"]')).toBeNull();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("renders a section the barrel alone registers (Background agents), never the unregistered Empty", async () => {
    // `background` is registered ONLY via the barrel — WorkView does not import it itself — so
    // its own body (the Manage subagents link) appearing proves the barrel wiring took effect.
    useUI.setState({ surfaceStates: { work: { section: "background" } } });
    await render();
    expect(activeSection()).toContain("Background agents");
    expect(host.querySelector(".work-background")).not.toBeNull();
    expect(button("Manage subagents")).toBeDefined();
    expect(host.textContent).not.toContain("to show"); // the "No … to show" unregistered fallback
  });

  it("keeps §5.2's empty-state contract once mounted: only Goals offers a create control", async () => {
    // The mount is where the Work view replaces WorkPanel, so it's where WorkPanel's per-section
    // quick-adds (work-add-watch / work-add-task / work-add-schedule) stop being reachable. That
    // removal is by design: work-view.md §5.2 says the section empty state is a bare `Empty` and
    // "No other section has an action" — only Goals adds a button (Q15). Watches, tasks and
    // schedules are agent-created (goal/watch creation also stays on the /goal new & /watch new
    // chat commands). This locks that contract at the mounted surface so a quick-add can't creep
    // back in unnoticed. The persistent "This chat" filter button shares the list pane, so drop it.
    const listActions = () =>
      [...host.querySelectorAll(".work-view__list button")]
        .map((el) => el.textContent?.trim() ?? "")
        .filter((text) => text && text !== "This chat");

    useUI.setState({ surfaceStates: { work: { section: "goals" } } });
    await render();
    expect(host.querySelector(".work-view__list")?.textContent).toContain("No goals");
    expect(listActions()).toContain("New goal");

    for (const [section, emptyCopy] of [
      ["watches", "No watches"],
      ["tasks", "No tasks"],
      ["schedules", "No schedules"],
    ] as const) {
      useUI.setState({ surfaceStates: { work: { section } } });
      await render();
      expect(host.querySelector(".work-view__list")?.textContent, section).toContain(emptyCopy);
      expect(listActions(), section).toEqual([]);
    }
  });

  it("Expand opens Work in the modal and Dock to rail brings it back with the same section", async () => {
    useUI.setState({ surfaceStates: { work: { section: "background" } } });
    await render();
    expect(activeSection()).toContain("Background agents");

    // Expand → the SAME view opens in the shell modal, still on the Background section.
    expand();
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(useUI.getState().surfaceModal?.viewId).toBe("work");
    expect(dialog!.querySelector(".work-view__panes")).not.toBeNull();
    expect(dialog!.querySelector(".work-background")).not.toBeNull();

    // Dock to rail → modal clears, Work is the active right panel, and the section is preserved.
    await click("Dock to rail");
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(useUI.getState().surfaceModal).toBeNull();
    expect(useUI.getState().rightPanel).toBe("work");
    expect(useUI.getState().surfaceStates.work.section).toBe("background");
    expect(host.querySelector(".work-background")).not.toBeNull();
    expect(activeSection()).toContain("Background agents");
  });
});
