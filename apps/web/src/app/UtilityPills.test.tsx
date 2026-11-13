import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  // Several subscribers share a topic now that the Feed dialog (ActivitySurface) and its
  // pill both listen to `inbox.item`/`activity.message` — a single-slot map would let the
  // dialog's subscription overwrite (and on close, delete) the pill's.
  const listeners = new Map<string, Array<(data: Record<string, unknown>) => void>>();
  const subscribe = (topic: string, fn: (data: Record<string, unknown>) => void) => {
    listeners.set(topic, [...(listeners.get(topic) ?? []), fn]);
    return () => {
      listeners.set(topic, (listeners.get(topic) ?? []).filter((f) => f !== fn));
    };
  };
  return { listeners, subscribe, pendingCount: 2, openPanel: vi.fn() };
});
vi.mock("../lib/events", () => ({
  onServerEvent: mocks.subscribe,
  onTopic: mocks.subscribe,
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
// The Feed dialog body (ActivitySurface) pulls the heavy markdown pipeline + the
// full-screen reader — stub both so this suite stays fast and deterministic.
vi.mock("../chat/LazyMarkdown", () => ({
  Markdown: ({ children }: { children: string }) => <div className="md">{children}</div>,
}));
vi.mock("../docviewer", () => ({ openDocument: vi.fn() }));

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
  act(() => (mocks.listeners.get(topic) ?? []).forEach((fn) => fn({})));
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

it("counts unseen Feed items, clears on opening its OWN dialog, and resumes after close", async () => {
  vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
  vi.spyOn(api, "activity").mockResolvedValue({ context_id: "s1", entries: [], messages: [] });
  // The Feed dialog body reads the agent's display name off the shared runtime probe.
  vi.spyOn(api, "runtimeStatus").mockResolvedValue(
    { identity: { name: "protoAgent", operator: "" } } as unknown as Awaited<ReturnType<typeof api.runtimeStatus>>,
  );
  act(() =>
    root.render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ActivityWidget />
      </QueryClientProvider>,
    ),
  );
  expect(pill("activity-badge")).toBeNull();
  emit("activity.message");
  emit("inbox.item");
  expect(pill("activity-badge").textContent).toBe("2");
  await act(async () => pill("activity-widget").click());
  // The Feed is its own home since S4c removed Work's Activity section: the pill opens
  // the full ActivitySurface in its own dialog and never touches the Work modal.
  expect(useUI.getState().surfaceModal).toBeNull();
  const dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain("Feed");
  expect(dialog.querySelector('[data-testid="unified-feed"]')).toBeTruthy();
  expect(dialog.querySelector('[data-testid="feed-list"]')).toBeTruthy();
  expect(pill("activity-badge")).toBeNull();
  // Events arriving while the dialog is open stay read.
  emit("activity.message");
  expect(pill("activity-badge")).toBeNull();
  // Opening the Work modal neither opens nor clears the feed — they are independent now.
  act(() => useUI.getState().openSurfaceModal("work", { section: "background" }));
  expect(document.querySelector('[role="dialog"] [data-testid="unified-feed"]')).toBeTruthy();
  act(() => useUI.getState().closeSurfaceModal());
  // Closing the dialog resumes unread tracking.
  await act(async () => document.querySelector<HTMLButtonElement>(".pl-dialog__close")!.click());
  expect(document.querySelector('[data-testid="unified-feed"]')).toBeNull();
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
    (mocks.listeners.get("background.progress") ?? []).forEach((fn) => fn({ job_id: job.id, ...data }));
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
