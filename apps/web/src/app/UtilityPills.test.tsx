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
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    mocks.listeners.set(topic, fn);
    return () => mocks.listeners.delete(topic);
  },
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
import { useBackgroundProgress } from "./BackgroundToolFeed";
import { sections } from "../work/sectionRegistry";
import type { WorkItems } from "../work/useWorkItems";
import "../work/sections/background";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  useUI.setState({ surfaceModal: null, backgroundJobsRequest: 0 });
  useBackgroundProgress.setState({ jobs: {} });
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
// The pills count across every chat, so they open their section on All (Josh 2026-11-11).
const modal = (section: string) => expect(useUI.getState().surfaceModal).toEqual({
  viewId: "work", state: { section, filters: { thisChat: false } },
});
function emit(topic: string) {
  act(() => mocks.listeners.get(topic)?.({}));
}

it("routes the Decisions utility entry to Work and preserves pending badges", () => {
  const render = () => root.render(
    <DecisionsWidget onOpen={() => useUI.getState().openSurfaceModal("work", { section: "decisions", filters: { thisChat: false } })} />,
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


it("keeps earlier tool outputs and live progress in the background detail opened from the pill", async () => {
  const job = { id: "job-1", status: "running" as const, subagent_type: "coder", description: "Build the report" };
  vi.spyOn(api, "background").mockResolvedValue({ enabled: true, jobs: [job] });
  const empty = { items: [], status: "ready" as const, error: null, loadedAt: 1, retry: vi.fn() };
  const sources: WorkItems = {
    decisions: empty, goals: empty, watches: empty, tasks: empty,
    schedules: empty, activity: empty, directives: empty,
    background: {
    items: [{ id: job.id, kind: "background", status: "running", title: job.description, meta: "", raw: job }],
    status: "ready", error: null, loadedAt: 1, retry: vi.fn(),
  } };
  function Harness() {
    const modal = useUI((state) => state.surfaceModal);
    return <>
      <BackgroundJobs />
      {modal ? sections.get("background")!.render({
        sources, state: modal.state || {}, onBack: vi.fn(),
        onOpen: (item) => useUI.getState().openSurfaceModal("work", { section: "background", itemId: item.id }),
      }) : null}
    </>;
  }
  const progress = (data: Record<string, unknown>) => act(() => {
    mocks.listeners.get("background.progress")?.({ job_id: job.id, ...data });
  });
  await act(async () => root.render(<Harness />));
  progress({ phase: "tool_end", tool: "read_file", tool_call_id: "read", output: "Earlier file contents" });
  progress({ phase: "tool_start", tool: "run_tests", tool_call_id: "tests" });
  progress({ job_id: "other-job", phase: "tool_start", tool: "unrelated_tool", tool_call_id: "other" });
  await act(async () => pill("background-jobs-pill").click());
  await act(async () => container.querySelector<HTMLButtonElement>(".work-row")!.click());
  const feed = () => container.querySelector(".bg-jobs-feed")!;
  expect(feed().textContent).toContain("read_file");
  expect(feed().textContent).toContain("run_tests");
  expect(feed().textContent).not.toContain("unrelated_tool");
  // Open the actual DS tool cards to read their output, including a result arriving live.
  act(() => feed().querySelectorAll<HTMLButtonElement>("button").forEach((button) => button.click()));
  expect(feed().textContent).toContain("Earlier file contents");
  progress({ phase: "tool_end", tool: "run_tests", tool_call_id: "tests", error: true, output: "Test failed: missing fixture" });
  act(() => Array.from(feed().querySelectorAll<HTMLButtonElement>("button")).find((button) => button.textContent?.includes("run_tests"))!.click());
  expect(feed().textContent).toContain("Test failed: missing fixture");
  expect(feed().querySelectorAll("button")).toHaveLength(2);
  // Switching sections and returning retains the same history.
  act(() => useUI.getState().closeSurfaceModal());
  await act(async () => pill("background-jobs-pill").click());
  await act(async () => container.querySelector<HTMLButtonElement>(".work-row")!.click());
  act(() => feed().querySelectorAll<HTMLButtonElement>("button").forEach((button) => button.click()));
  expect(feed().textContent).toContain("Test failed: missing fixture");
});
