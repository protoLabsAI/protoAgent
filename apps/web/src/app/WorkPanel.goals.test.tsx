// Work ▸ Goals card: a goal must visibly GO GREEN. It used to drop from "1 driving · 0/8"
// straight to "No active goals" the moment its verifier passed, because the card listed
// active goals only. Now finished goals stay under a "Recent" divider (achieved = green
// check + verifier summary), the count stays the ACTIVE count, and a `goal.changed` push
// flips the card live. createRoot/act + real providers, like ThemeSurface.test.tsx.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listeners: new Map<string, Array<(data: Record<string, unknown>) => void>>(),
}));

vi.mock("../lib/events", () => ({
  onServerEvent: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    const arr = mocks.listeners.get(topic) ?? [];
    arr.push(fn);
    mocks.listeners.set(topic, arr);
    return () => {
      mocks.listeners.set(topic, (mocks.listeners.get(topic) ?? []).filter((f) => f !== fn));
    };
  },
}));

import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import type { GoalState } from "../lib/types";
import { resetDismissedGoals } from "../goals/dismissedGoals";
import { WorkPanel } from "./WorkPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

const nowS = () => Date.now() / 1000;
const driving: GoalState = {
  session_id: "s1",
  condition: "make the tests pass",
  status: "active",
  verifier: { type: "command", command: "pytest -q" },
  iteration: 2,
  max_iterations: 8,
  started_at: nowS() - 120,
};

beforeEach(() => {
  mocks.listeners.clear();
  resetDismissedGoals();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

async function mount(goals: GoalState[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(queryKeys.goals, { goals, enabled: true });
  client.setQueryData(queryKeys.watches, { watches: [], enabled: true });
  client.setQueryData(queryKeys.tasks, { issues: [] });
  client.setQueryData(queryKeys.schedules, { jobs: [] });
  await act(async () => {
    root.render(
      h(QueryClientProvider, { client }, h(ToastProvider, null, h(WorkPanel, { confirm: vi.fn() as never }))),
    );
  });
}

const card = () => container.querySelector('[data-testid="work-card-goals"]') as HTMLElement;
const badge = () => card().querySelector(".pl-badge")?.textContent?.trim();

describe("Work ▸ Goals card", () => {
  it("counts a driving goal as active — no false 'No active goals'", async () => {
    await mount([driving]);
    expect(card().textContent).not.toContain("No active goals");
    expect(badge()).toBe("1");
    expect(card().querySelectorAll('[data-testid="work-goal-active"]')).toHaveLength(1);
    expect(card().textContent).toContain("1 driving · iteration 2/8");
  });

  it("keeps an achieved goal under Recent with a success state and the verifier summary", async () => {
    await mount([{ ...driving, status: "achieved", finished_at: nowS() - 30, last_reason: "command exited 0" }]);
    expect(card().textContent).not.toContain("No active goals");
    expect(badge()).toBe("0"); // the count is ACTIVE goals; the finished one sits under Recent
    const row = card().querySelector('[data-testid="work-goal-recent"]') as HTMLElement;
    expect(row.dataset.status).toBe("achieved");
    expect(row.className).toContain("work-row--goal-achieved");
    expect(row.textContent).toContain("achieved · command: pytest -q · just now");
    expect(card().textContent).toContain("Recent");
  });

  it("shows a failed goal with its state and reason", async () => {
    await mount([
      { ...driving, status: "exhausted", finished_at: nowS() - 300, last_reason: "ran out of iteration budget (8)" },
    ]);
    const row = card().querySelector('[data-testid="work-goal-recent"]') as HTMLElement;
    expect(row.dataset.status).toBe("exhausted");
    expect(row.className).toContain("work-row--goal-failed");
    expect(row.textContent).toContain("exhausted · ran out of iteration budget (8) · 5m ago");
  });

  it("drops a finished goal past the recent window back to the empty state", async () => {
    await mount([{ ...driving, status: "achieved", finished_at: nowS() - 2 * 3600 }]);
    expect(card().textContent).toContain("No active goals");
  });

  it("dismissing a recent goal hides it without opening the panel", async () => {
    await mount([{ ...driving, status: "achieved", finished_at: nowS() - 10 }]);
    const dismiss = card().querySelector(".work-row-dismiss") as HTMLButtonElement;
    await act(async () => dismiss.click());
    expect(card().querySelector('[data-testid="work-goal-recent"]')).toBeNull();
    expect(card().textContent).toContain("No active goals");
    // Still on the overview — the × didn't navigate into the Goals panel.
    expect(container.querySelector('[data-testid="work-back"]')).toBeNull();
  });

  it("flips driving → achieved live on the goal.changed push", async () => {
    await mount([driving]);
    expect(card().querySelectorAll('[data-testid="work-goal-active"]')).toHaveLength(1);

    const achieved = { ...driving, status: "achieved", finished_at: nowS(), last_reason: "command exited 0" };
    vi.spyOn(api, "goals").mockResolvedValue({ goals: [achieved], enabled: true } as never);
    await act(async () => {
      (mocks.listeners.get("goal.changed") ?? []).forEach((fn) => fn({ session_id: "s1" }));
    });
    await vi.waitFor(async () => {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 5));
      });
      expect(card().querySelectorAll('[data-testid="work-goal-active"]')).toHaveLength(0);
    });
    const row = card().querySelector('[data-testid="work-goal-recent"]') as HTMLElement;
    expect(row?.dataset.status).toBe("achieved");
  });
});
