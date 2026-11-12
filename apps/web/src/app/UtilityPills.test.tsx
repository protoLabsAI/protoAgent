import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  // Multiple subscribers share a topic (the Feed pill AND its dialog's ActivitySurface),
  // so each topic fans out to a Set — one unmount must not silence the others.
  listeners: new Map<string, Set<(data: Record<string, unknown>) => void>>(),
  pendingCount: 2,
  openPanel: vi.fn(),
}));
vi.mock("../lib/events", () => {
  const subscribe = (topic: string, fn: (data: Record<string, unknown>) => void) => {
    const set = mocks.listeners.get(topic) ?? new Set<(data: Record<string, unknown>) => void>();
    set.add(fn);
    mocks.listeners.set(topic, set);
    return () => set.delete(fn);
  };
  return {
    onServerEvent: subscribe,
    onTopic: subscribe,
    onConnectionChange: () => () => {},
  };
});
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
// jsdom has no Element.scrollTo; the Feed dialog pins its newest entry into view with it.
Element.prototype.scrollTo ??= () => {};
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
const modal = (section: string) => expect(useUI.getState().surfaceModal).toEqual({
  viewId: "work", state: { section },
});
function emit(topic: string) {
  act(() => mocks.listeners.get(topic)?.forEach((fn) => fn({})));
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

it("counts unseen Activity items, clears in its own Feed dialog, and resumes after closing", async () => {
  // History left Work with the four-group consolidation (ADR 0129 Q3): the pill opens the
  // widget's own Feed dialog now, never the Work modal.
  vi.spyOn(api, "activity").mockResolvedValue({ context_id: "activity", messages: [], entries: [] });
  vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
  await act(async () => root.render(<ActivityWidget />));
  expect(pill("activity-badge")).toBeNull();
  emit("activity.message");
  emit("inbox.item");
  expect(pill("activity-badge").textContent).toBe("2");
  await act(async () => pill("activity-widget").click());
  expect(useUI.getState().surfaceModal).toBeNull();
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("Feed");
  expect(document.querySelector('[data-testid="unified-feed"]')).not.toBeNull();
  expect(pill("activity-badge")).toBeNull();
  emit("activity.message");
  expect(pill("activity-badge")).toBeNull(); // read while the feed is open
  const close = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
    .find((node) => node.getAttribute("aria-label") === "Close");
  expect(close).toBeTruthy();
  await act(async () => close!.click());
  emit("activity.message");
  expect(pill("activity-badge").textContent).toBe("1");
  // A legacy saved Work `activity` section still marks the feed read when something opens it.
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
    mocks.listeners.get("background.progress")?.forEach((fn) => fn({ job_id: job.id, ...data }));
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
