import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { chatStore } from "./chat-store";
import { useUI } from "../state/uiStore";
import { DirectiveStrip, DirectiveWorkRequest } from "./DirectiveStrip";
import { GoalRunStrip } from "./GoalRunStrip";
import { BackgroundWorkStrip } from "./BackgroundWorkStrip";
import type { GoalState } from "../lib/types";

const data = vi.hoisted(() => ({
  goals: [] as GoalState[],
  refresh: vi.fn(),
  failed: false,
  dismiss: vi.fn(),
}));
vi.mock("./useDirectives", () => ({ useDirectives: () => ({
  state: data.failed ? "failed" : "ready", active: [{ quote: "Keep it small" }], refresh: data.refresh,
}) }));
vi.mock("../lib/useIsMobile", () => ({ useIsMobile: () => false }));
vi.mock("@tanstack/react-query", async (importOriginal) => ({
  ...await importOriginal<typeof import("@tanstack/react-query")>(),
  useQuery: () => ({ data: { goals: data.goals } }),
}));
vi.mock("../goals/useNow", () => ({ useNow: () => 1000000 }));
vi.mock("../goals/dismissedGoals", () => ({ useDismissedGoals: () => new Set(), dismissGoal: data.dismiss }));
vi.mock("./backgroundJobStore", () => ({ useRunningBackgroundJobs: () => [{ description: "Working" }] }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let host: HTMLDivElement;
const initial = useUI.getState();
beforeEach(() => {
  vi.clearAllMocks();
  data.failed = false;
  data.goals = [{ session_id: "here", condition: "Ship it", status: "active" } as GoalState];
  useUI.setState({ ...initial, rightPanel: "context", rightCollapsed: true, surfaceModal: null,
    mobileActive: "chat", railOrder: { left: ["chat"], right: ["work", "context"], bottom: [], hidden: [] },
    directivesRequest: null, surfaceStates: { work: { section: "tasks", itemId: "task:old", scrollTop: 80,
      filters: { thisChat: false, retained: true } } } });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount()); host.remove(); useUI.setState(initial);
});
async function render(node: ReactElement) { await act(async () => root.render(node)); }
async function click(label: string) {
  const button = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
  expect(button).toBeTruthy();
  await act(async () => button!.click());
}
function expectWork(section: string, itemId?: string) {
  const ui = useUI.getState();
  expect(ui.mobileActive).toBe("work");
  expect(ui.railOrder.hidden).not.toContain("work");
  if (ui.railOrder.left.includes("work")) {
    expect(ui.surface).toBe("work");
    expect(ui.leftCollapsed).toBe(false);
  } else if (ui.railOrder.bottom.includes("work")) {
    expect(ui.bottomPanel).toBe("work");
    expect(ui.bottomCollapsed).toBe(false);
    expect(ui.bottomHeight).toBeGreaterThanOrEqual(120);
  } else {
    expect(ui.railOrder.right).toContain("work");
    expect(ui.rightPanel).toBe("work");
    expect(ui.rightCollapsed).toBe(false);
    expect(ui.rightWidth).toBeGreaterThanOrEqual(280);
  }
  expect(ui.surfaceModal).toBeNull();
  expect(ui.surfaceStates.work).toEqual({ section, itemId, scrollTop: 0, filters: { thisChat: true, retained: true } });
  expect(ui.backgroundJobsRequest).toBe(initial.backgroundJobsRequest);
  expect(ui.directivesRequest).toBeNull();
}
it.each([true, false])("opens Directives in the rail (collapsed=%s)", async (rightCollapsed) => {
  useUI.setState({ rightCollapsed });
  await render(<DirectiveStrip sessionId="here" />); await click("View"); expectWork("directives");
});
it("retries a failed directive load without navigating", async () => {
  data.failed = true;
  await render(<DirectiveStrip sessionId="here" />); await click("Retry");
  expect(data.refresh).toHaveBeenCalledOnce(); expect(useUI.getState().rightPanel).toBe("context");
});
it.each(["active", "achieved", "exhausted"] as const)("opens the chat's %s goal details", async (status) => {
  data.goals[0] = { ...data.goals[0], status, finished_at: status === "active" ? null : 999 };
  data.goals.unshift({ session_id: "other", condition: "Elsewhere", status: "active" } as GoalState);
  await render(<GoalRunStrip sessionId="here" />); await click("View"); expectWork("goals", "goal:here");
});
it("keeps goal dismissal separate from navigation", async () => {
  data.goals[0] = { ...data.goals[0], status: "achieved", finished_at: 999 };
  await render(<GoalRunStrip sessionId="here" />);
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Dismiss finished goal"]')!.click());
  expect(data.dismiss).toHaveBeenCalledOnce(); expect(useUI.getState().rightPanel).toBe("context");
});
it("opens Background in the rail", async () => {
  await render(<BackgroundWorkStrip sessionId="here" />); await click("View"); expectWork("background");
});
it("consumes repeated /directives requests only for the matching chat", async () => {
  await render(<><DirectiveWorkRequest sessionId="other" /><DirectiveWorkRequest sessionId="here" /></>);
  for (let i = 0; i < 2; i++) {
    await act(async () => {
      useUI.setState({ rightCollapsed: true, rightPanel: "context" });
      useUI.getState().openDirectives("here");
    });
    expectWork("directives");
  }
  expect(host.childElementCount).toBe(0);
});
it("leaves requests for other chats unconsumed", async () => {
  await render(<DirectiveWorkRequest sessionId="other" />);
  await act(async () => useUI.getState().openDirectives("here"));
  expect(useUI.getState().directivesRequest?.sessionId).toBe("here");
  expect(useUI.getState().rightPanel).toBe("context");
});

for (const dock of ["left", "right", "bottom", "hidden"] as const) {
  it.each(["directives", "goals", "background", "request"] as const)(
    `reveals %s from ${dock}, including mobile navigation and collapsed zero-size docks`, async (entry) => {
      const railOrder = { left: ["chat"], right: ["context"], bottom: [] as string[], hidden: [] as string[] };
      railOrder[dock].push("work");
      useUI.setState({ railOrder, leftCollapsed: true, rightCollapsed: true, bottomCollapsed: true,
        rightWidth: 0, bottomHeight: 0 });
      if (entry === "request") {
        await render(<DirectiveWorkRequest sessionId="here" />);
        await act(async () => useUI.getState().openDirectives("here"));
      } else {
        const Strip = entry === "directives" ? DirectiveStrip : entry === "goals" ? GoalRunStrip : BackgroundWorkStrip;
        await render(<Strip sessionId="here" />);
        await click("View");
      }
      expectWork(entry === "request" ? "directives" : entry, entry === "goals" ? "goal:here" : undefined);
    },
  );
}

it("selects a previously visited inactive chat before consuming its directive request", async () => {
  const target = chatStore.createSession();
  const current = chatStore.createSession({ incognito: true });
  expect(current.id).not.toBe(target.id);
  expect(chatStore.getSnapshot().currentSessionId).toBe(current.id);
  await render(<><DirectiveWorkRequest sessionId={target.id} /><DirectiveWorkRequest sessionId={current.id} /></>);
  await act(async () => useUI.getState().openDirectives(target.id));
  expect(chatStore.getSnapshot().currentSessionId).toBe(target.id);
  expectWork("directives");
});
