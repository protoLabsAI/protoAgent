// A HITL answer continues the task that parked (A2A §3.4.3, #3930): the console sends
// the parked task's id on the SendStreamingMessage it answers with.
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";

function captureBody() {
  const bodies: Array<{ params: { message: Record<string, unknown> } }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit) => {
      bodies.push(JSON.parse(String(init.body)));
      return new Response("", { status: 200, headers: { "content-type": "text/event-stream" } });
    }),
  );
  return bodies;
}

afterEach(() => vi.unstubAllGlobals());

describe("streamChat: the HITL answer names its parked task", () => {
  it("sends taskId with a hitlResume answer", async () => {
    const bodies = captureBody();
    await api.streamChat("banana", "chat-1", {}, { hitlResume: true, taskId: "parked-1" });
    expect(bodies[0].params.message).toMatchObject({
      contextId: "chat-1",
      taskId: "parked-1",
      metadata: { hitl_resume: true },
    });
  });

  it("never sends taskId on an ordinary message (a new turn is a new task)", async () => {
    const bodies = captureBody();
    await api.streamChat("hello", "chat-1", {}, { taskId: "parked-1" });
    expect(bodies[0].params.message.taskId).toBeUndefined();
  });
});
