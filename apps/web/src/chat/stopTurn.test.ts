import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import { finalizeStoppedMessages, resolveStopTarget } from "./stopTurn";

const msg = (over: Partial<ChatMessage>): ChatMessage => ({
  role: "assistant",
  content: "",
  ...over,
});

describe("resolveStopTarget", () => {
  it("prefers the slot's live taskId when it owns the turn", () => {
    const messages = [msg({ id: "a1", status: "streaming", taskId: "task-old" })];
    expect(resolveStopTarget(messages, "task-live")).toBe("task-live");
  });

  it("falls back to the streaming message's durable taskId when the slot re-attached (#1617)", () => {
    const messages = [
      msg({ id: "a1", status: "done", taskId: "task-done" }),
      msg({ id: "a2", status: "streaming", taskId: "task-live" }),
    ];
    expect(resolveStopTarget(messages, "")).toBe("task-live");
  });

  it("picks the MOST RECENT streaming assistant message", () => {
    const messages = [
      msg({ id: "a1", status: "streaming", taskId: "task-stale" }),
      msg({ id: "a2", status: "streaming", taskId: "task-current" }),
    ];
    expect(resolveStopTarget(messages, "")).toBe("task-current");
  });

  it("ignores user/system messages and returns empty when nothing streams", () => {
    const messages = [
      { role: "user" as const, content: "hi", status: "streaming" as const, taskId: "not-a-turn" },
      msg({ id: "a1", status: "done", taskId: "task-done" }),
    ];
    expect(resolveStopTarget(messages, "")).toBe("");
  });

  it("returns empty for a streaming message that never got a task id", () => {
    expect(resolveStopTarget([msg({ id: "a1", status: "streaming" })], "")).toBe("");
  });
});

describe("finalizeStoppedMessages", () => {
  it("flips every streaming assistant bubble to done, keeping partial content", () => {
    const out = finalizeStoppedMessages([
      msg({ id: "a1", status: "streaming", content: "partial answer" }),
      msg({ id: "a2", status: "done", content: "finished" }),
    ]);
    expect(out[0]).toMatchObject({ status: "done", content: "partial answer" });
    expect(out[1]).toMatchObject({ status: "done", content: "finished" });
  });

  it("settles running tool cards on the stopped bubble", () => {
    const out = finalizeStoppedMessages([
      msg({
        id: "a1",
        status: "streaming",
        toolCalls: [
          { id: "t1", name: "search", status: "running" },
          { id: "t2", name: "read", status: "error" },
        ],
      }),
    ]);
    expect(out[0].toolCalls).toEqual([
      { id: "t1", name: "search", status: "done" },
      { id: "t2", name: "read", status: "error" },
    ]);
  });

  it("leaves user messages and non-streaming bubbles untouched (same reference)", () => {
    const user = { role: "user" as const, content: "hi" };
    const done = msg({ id: "a1", status: "error", content: "failed" });
    const out = finalizeStoppedMessages([user, done]);
    expect(out[0]).toBe(user);
    expect(out[1]).toBe(done);
  });
});
