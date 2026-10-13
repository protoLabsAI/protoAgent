// PaletteChat's reopen self-heal (ADR 0057, #3972). A palette closed mid-turn leaves the
// last assistant message `streaming` with its taskId; on reopen the palette GetTasks it,
// finalizing when the task has settled and polling every 3s while it runs. These render the
// REAL component over a mocked `api.getTask`: a REJECTED turn settles as an error, and an
// `unknown` / UNSPECIFIED one settles on the first poll instead of locking the composer for
// the whole ~2-minute poll budget.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getTask: vi.fn() }));

vi.mock("../lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/api")>();
  return { ...real, api: { ...real.api, getTask: mocks.getTask } };
});

import type { ChatMessage } from "../lib/types";
import { PaletteChat } from "./PaletteChat";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const KEY = "protoagent.palette.chat";
const TASK = "task-palette-1";
let container: HTMLElement;
let root: Root | null;

function seedInterruptedTurn() {
  localStorage.setItem(
    KEY,
    JSON.stringify({
      contextId: "palette-ctx",
      messages: [
        { role: "user", content: "hi", status: "done" },
        { role: "assistant", content: "partial", status: "streaming", taskId: TASK, toolCalls: [] },
      ],
    }),
  );
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
  act(() => root!.render(h(PaletteChat, { agentName: "protoAgent" })));
  await settle();
}

/** The transcript as the palette flushes it on close (the unmount flush is immediate). */
function closeAndRead(): ChatMessage[] {
  act(() => root!.unmount());
  root = null;
  return JSON.parse(localStorage.getItem(KEY) || "{}").messages;
}

const lastOf = (messages: ChatMessage[]) => messages[messages.length - 1];

beforeEach(() => {
  // jsdom has no ResizeObserver; the kit's Conversation pins its scroll with one.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.useFakeTimers();
  localStorage.clear();
  mocks.getTask.mockReset();
  seedInterruptedTurn();
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("PaletteChat reopen self-heal", () => {
  it("settles a REJECTED turn as an error", async () => {
    mocks.getTask.mockResolvedValue({ state: "TASK_STATE_REJECTED", text: "" });
    await mount();
    const last = lastOf(closeAndRead());
    expect(last.status).toBe("error");
    expect(last.content).toBe("partial");
  });

  it("settles a completed turn as done with the task's text", async () => {
    mocks.getTask.mockResolvedValue({ state: "TASK_STATE_COMPLETED", text: "the full answer" });
    await mount();
    const last = lastOf(closeAndRead());
    expect(last).toMatchObject({ status: "done", content: "the full answer" });
  });

  for (const state of ["TASK_STATE_UNSPECIFIED", "unknown"]) {
    it(`settles a ${state} turn on the first poll instead of polling it`, async () => {
      mocks.getTask.mockResolvedValue({ state, text: "" });
      await mount();
      expect(mocks.getTask).toHaveBeenCalledTimes(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
      expect(mocks.getTask).toHaveBeenCalledTimes(1);
      // Unlocked: the composer is no longer held by a reconcile that cannot end.
      const last = lastOf(closeAndRead());
      expect(last.status).toBe("done");
      expect(last.content).toBe("partial");
    });
  }

  it("keeps polling a turn that is still working", async () => {
    mocks.getTask.mockResolvedValue({ state: "TASK_STATE_WORKING", text: "" });
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_500);
    });
    expect(mocks.getTask.mock.calls.length).toBeGreaterThanOrEqual(4);
    expect(lastOf(closeAndRead()).status).toBe("streaming");
  });
});
