// The unary GetTask readers all unwrap one result shape (#3957). A2A 1.0 serves the task
// flat and untagged, 0.3 tags it `kind: "task"`, and a `{task}` wrapper is read too. Each
// reader used to spell the unwrap `result.task ?? (result.kind === "task" ? result : result)`
// — both arms `result` — so a result tagged as something else (a 0.3 status update, a
// message) was read as the task: a stray `status-update` carrying `completed` settled a
// turn off a frame that is not the task at all.
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";

function serveGetTask(result: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify({ jsonrpc: "2.0", id: "get", result }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ),
  );
}

afterEach(() => vi.unstubAllGlobals());

const TASK = {
  id: "task-1",
  contextId: "chat-1",
  status: { state: "TASK_STATE_COMPLETED" },
  artifacts: [{ parts: [{ text: "the answer" }] }],
};

describe("GetTask result shapes (#3957)", () => {
  it("reads the A2A 1.0 task served flat and untagged", async () => {
    serveGetTask(TASK);
    expect(await api.getTask("task-1")).toMatchObject({ state: "TASK_STATE_COMPLETED", text: "the answer" });
  });

  it("reads the A2A 0.3 task tagged kind: task", async () => {
    serveGetTask({ ...TASK, kind: "task", status: { state: "completed" } });
    expect(await api.getTask("task-1")).toMatchObject({ state: "completed", text: "the answer" });
  });

  it("reads a {task} wrapper", async () => {
    serveGetTask({ task: TASK });
    expect(await api.getTask("task-1")).toMatchObject({ state: "TASK_STATE_COMPLETED", text: "the answer" });
  });

  it("never reads a result tagged as something else as the task", async () => {
    serveGetTask({ kind: "status-update", taskId: "task-1", status: { state: "completed" } });
    expect(await api.getTask("task-1")).toEqual({ state: "", text: "" });

    serveGetTask({ kind: "status-update", taskId: "task-1", status: { state: "completed" } });
    expect(await api.taskSteerState("task-1")).toEqual({ state: "", consumed: [] });

    serveGetTask({ kind: "status-update", taskId: "task-1", status: { state: "completed" } });
    expect(await api.replayTask("task-1", "chat-1")).toBe("");

    serveGetTask({ kind: "message", messageId: "m1", parts: [{ text: "hi" }] });
    expect(await api.getTaskTurn("task-1")).toBeNull();
  });

  it("replays and reads the same task through every reader", async () => {
    serveGetTask({ ...TASK, kind: "task" });
    expect(await api.replayTask("task-1", "chat-1")).toBe("TASK_STATE_COMPLETED");
    serveGetTask(TASK);
    expect(await api.taskSteerState("task-1")).toEqual({ state: "TASK_STATE_COMPLETED", consumed: [] });
    serveGetTask({ task: TASK });
    expect(await api.getTaskTurn("task-1")).toMatchObject({ task_id: "task-1", state: "TASK_STATE_COMPLETED" });
  });
});
