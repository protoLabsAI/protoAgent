import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listeners: new Map<string, (data: Record<string, unknown>) => void>(),
  pendingCount: 2,
  openPanel: vi.fn(),
}));
vi.mock("../lib/events", () => ({
  onServerEvent: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    mocks.listeners.set(topic, fn);
    return () => mocks.listeners.delete(topic);
  },
  onTopic: () => () => {},
  onConnectionChange: () => () => {},
}));
vi.mock("../decisions/useDecisions", () => ({
  useDecisions: () => ({
    decisions: [], pendingCount: mocks.pendingCount, top: null, loaded: true,
    panelOpen: false, openPanel: mocks.openPanel, closePanel: vi.fn(), answer: vi.fn(),
  }),
}));
vi.mock("@protolabsai/ui/overlays", async (original) => ({
  ...(await original<object>()), useToast: () => vi.fn(),
}));

import { api } from "../lib/api";
import { useUI } from "../state/uiStore";
import { DecisionsWidget } from "../decisions/DecisionsWidget";
import { ActivityWidget } from "../activity/ActivityWidget";
import { BackgroundJobs } from "./BackgroundJobs";
import { UtilityWidget } from "./UtilityWidget";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  useUI.setState({ surfaceModal: null, backgroundJobsRequest: 0 });
  localStorage.clear();
  mocks.pendingCount = 2;
  mocks.openPanel.mockClear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  mocks.listeners.clear();
  vi.restoreAllMocks();
});
const pill = (id: string) => container.querySelector<HTMLButtonElement>(`[data-testid="${id}"]`)!;
const modal = (section: string) => expect(useUI.getState().surfaceModal).toEqual({
  viewId: "work", state: { section },
});
function emit(topic: string) {
  act(() => mocks.listeners.get(topic)?.({}));
}

it("routes the Decisions utility entry to Work and preserves pending badges", () => {
  const render = () => root.render(
    <DecisionsWidget onOpen={() => useUI.getState().openSurfaceModal("work", { section: "decisions" })} />,
  );
  act(render);
  expect(pill("decisions-widget").querySelector(".pl-count")?.textContent).toBe("2");
  act(() => pill("decisions-widget").click());
  modal("decisions");
  expect(mocks.openPanel).not.toHaveBeenCalled();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  mocks.pendingCount = 0;
  act(render);
  expect(pill("decisions-widget").querySelector(".pl-count")).toBeNull();
});

it.each([0, 2])("routes background jobs to Work with %i running jobs", async (running) => {
  vi.spyOn(api, "background").mockResolvedValue({
    enabled: true,
    jobs: Array.from({ length: running }, (_, i) => ({
      id: String(i), status: "running" as const, subagent_type: "agent", description: "working",
    })),
  });
  await act(async () => root.render(<BackgroundJobs />));
  const button = pill("background-jobs-pill");
  expect(button.querySelector(".pl-count")?.textContent ?? null).toBe(running ? "2" : null);
  expect(button.querySelector(".pl-dot--pulse") !== null).toBe(running > 0);
  await act(async () => button.click());
  modal("background");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});

it("counts unseen Activity items, clears on opening, and resumes after leaving the section", () => {
  act(() => root.render(<ActivityWidget />));
  expect(pill("activity-badge")).toBeNull();
  emit("activity.message");
  emit("inbox.item");
  expect(pill("activity-badge").textContent).toBe("2");
  act(() => pill("activity-widget").click());
  modal("activity");
  expect(pill("activity-badge")).toBeNull();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  emit("activity.message");
  expect(pill("activity-badge")).toBeNull();
  act(() => useUI.getState().openSurfaceModal("work", { section: "background" }));
  emit("activity.message");
  expect(pill("activity-badge").textContent).toBe("1");
  // Opening from another Work entry point also marks the section read.
  act(() => useUI.getState().openSurfaceModal("work", { section: "activity" }));
  expect(pill("activity-badge")).toBeNull();
  act(() => useUI.getState().closeSurfaceModal());
  emit("inbox.item");
  expect(pill("activity-badge").textContent).toBe("1");
});

it("keeps plugin utility widgets opening their own dialog", () => {
  act(() => root.render(
    <UtilityWidget testId="plugin" icon={null} label="Plugin" dialogTitle="Plugin">
      <p>Plugin content</p>
    </UtilityWidget>,
  ));
  act(() => pill("plugin").click());
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("Plugin content");
  expect(useUI.getState().surfaceModal).toBeNull();
});
