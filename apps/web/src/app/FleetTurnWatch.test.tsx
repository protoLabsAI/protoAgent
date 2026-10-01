// FleetTurnWatch — the cross-agent "turn finished" poller (#3972). It GetTasks another
// agent's in-flight turn every 5s and toasts when it settles. These drive the REAL
// component over a mocked fetch: a REJECTED turn must toast as an error, an `unknown` /
// UNSPECIFIED one must settle instead of being polled forever, and the GetTask result is
// unwrapped by `taskFromGetTask` — a tagged status update is not the task.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ toast: vi.fn(), notify: vi.fn() }));

vi.mock("@protolabsai/ui/overlays", () => ({ useToast: () => mocks.toast }));
vi.mock("../lib/notify", () => ({ notifyIfHidden: mocks.notify }));
vi.mock("../lib/api", () => ({
  api: { fleet: async () => ({ agents: [{ id: "ava", name: "Ava", host: false }] }) },
  authToken: () => "",
  currentSlug: () => "host",
}));

import { FleetTurnWatch } from "./FleetTurnWatch";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TASK = "task-ava-1";
let container: HTMLElement;
let root: Root;
let fetchMock: ReturnType<typeof vi.fn>;

function seedOtherAgentTurn() {
  localStorage.setItem(
    "protoagent.chat.sessions:ava",
    JSON.stringify({
      sessions: [
        {
          id: "s1",
          title: "Ava's chat",
          messages: [
            { id: "u1", role: "user", content: "go", status: "done" },
            { id: "a1", role: "assistant", content: "", status: "streaming", taskId: TASK },
          ],
        },
      ],
    }),
  );
}

function serveGetTask(result: unknown) {
  fetchMock.mockImplementation(async () => ({ ok: true, json: async () => ({ jsonrpc: "2.0", id: "x", result }) }));
}

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }
}

async function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(h(FleetTurnWatch)));
  await settle();
}

const getTaskCalls = () => fetchMock.mock.calls.filter(([, init]) => String(init?.body).includes('"GetTask"')).length;

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  sessionStorage.clear();
  mocks.toast.mockReset();
  mocks.notify.mockReset();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  seedOtherAgentTurn();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("FleetTurnWatch", () => {
  it("toasts a REJECTED turn as an error, not a clean finish", async () => {
    serveGetTask({ id: TASK, status: { state: "TASK_STATE_REJECTED" } });
    await mount();
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.toast.mock.calls[0][0]).toMatchObject({ tone: "error", title: "Ava finished a turn" });
    expect(mocks.toast.mock.calls[0][0].message).toContain("TASK_STATE_REJECTED");
  });

  for (const state of ["TASK_STATE_UNSPECIFIED", "unknown"]) {
    it(`settles a turn the server reports as ${state} instead of polling it forever`, async () => {
      serveGetTask({ id: TASK, status: { state } });
      await mount();
      // Settled on the first poll — announced once, and never asked about again.
      expect(mocks.toast).toHaveBeenCalledTimes(1);
      expect(getTaskCalls()).toBe(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(getTaskCalls()).toBe(1);
      expect(mocks.toast).toHaveBeenCalledTimes(1);
    });
  }

  it("keeps polling a turn that is still working", async () => {
    serveGetTask({ id: TASK, status: { state: "TASK_STATE_WORKING" } });
    await mount();
    expect(mocks.toast).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getTaskCalls()).toBeGreaterThanOrEqual(3);
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("unwraps GetTask with taskFromGetTask: a tagged status update is not the task", async () => {
    // `res?.task ?? res` read this as a completed task and announced a turn still running.
    serveGetTask({ kind: "status-update", taskId: TASK, status: { state: "TASK_STATE_COMPLETED" } });
    await mount();
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("reads a 0.3 `kind: \"task\"` result and a `{task}` wrapper as the task", async () => {
    serveGetTask({ kind: "task", id: TASK, status: { state: "completed" } });
    await mount();
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.toast.mock.calls[0][0]).toMatchObject({ tone: "success" });
    act(() => root.unmount());
    container.remove();

    sessionStorage.clear();
    mocks.toast.mockReset();
    serveGetTask({ task: { id: TASK, status: { state: "TASK_STATE_FAILED" } } });
    await mount();
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.toast.mock.calls[0][0]).toMatchObject({ tone: "error" });
  });
});
