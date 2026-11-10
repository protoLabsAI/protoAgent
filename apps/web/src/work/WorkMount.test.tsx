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
function activeTab() {
  return document.querySelector('[role="tab"][aria-selected="true"]')?.textContent ?? "";
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

  it("frames WorkView — not WorkPanel — in the right dock, with the Overview and nine section tabs", async () => {
    await render();
    // It's WorkView: its two-pane shell and section nav, never a WorkPanel overview card.
    expect(host.querySelector(".work-view__panes")).not.toBeNull();
    expect(host.querySelector('[data-testid^="work-card-"]')).toBeNull();
    // Overview plus the eight sources = nine entries in the nav.
    expect(host.querySelectorAll('[role="tab"]')).toHaveLength(9);
    expect(host.textContent).toContain("Nothing going on");
    // Framed by the rail host, so it carries the Expand control and no modal is open yet.
    expect(button("Expand")).toBeDefined();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("renders a section the barrel alone registers (Background agents), never the unregistered Empty", async () => {
    // `background` is registered ONLY via the barrel — WorkView does not import it itself — so
    // its own body (the Manage subagents link) appearing proves the barrel wiring took effect.
    useUI.setState({ surfaceStates: { work: { section: "background" } } });
    await render();
    expect(activeTab()).toContain("Background agents");
    expect(host.querySelector(".work-background")).not.toBeNull();
    expect(button("Manage subagents")).toBeDefined();
    expect(host.textContent).not.toContain("to show"); // the "No … to show" unregistered fallback
  });

  it("Expand opens Work in the modal and Dock to rail brings it back with the same section", async () => {
    useUI.setState({ surfaceStates: { work: { section: "background" } } });
    await render();
    expect(activeTab()).toContain("Background agents");

    // Expand → the SAME view opens in the shell modal, still on the Background section.
    await click("Expand");
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
    expect(activeTab()).toContain("Background agents");
  });
});
