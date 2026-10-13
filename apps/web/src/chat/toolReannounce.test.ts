import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";
import { applyText, applyToolEvent } from "./turnReducers";

// #3691: a second `start` for a card we already have — the native runtime's full-args
// re-announce, or an ACP coder's refined name/args — fills the card in. It must not
// render the card twice, re-nest it, or restart its clock.
const bubble = (): ChatMessage => ({ id: "A", role: "assistant", content: "", createdAt: 1, status: "streaming" });

describe("tool start re-announce", () => {
  afterEach(() => vi.useRealTimers());

  it("fills the card in without a second block when text arrived in between", () => {
    let m = bubble();
    m = applyToolEvent(m, { id: "t1", name: "Read File", phase: "start", input: "read" });
    m = applyText(m, "Looking at the file.", true);
    m = applyToolEvent(m, { id: "t1", name: "Read app.py", phase: "start", input: '{"file_path": "app.py"}' });

    expect(m.parts).toEqual([
      { kind: "tools", ids: ["t1"] },
      { kind: "text", text: "Looking at the file." },
    ]);
    expect(m.toolCalls).toHaveLength(1);
    expect(m.toolCalls?.[0]).toMatchObject({ name: "Read app.py", input: '{"file_path": "app.py"}', status: "running" });
  });

  it("keeps the card's original clock and nesting", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    let m = bubble();
    m = applyToolEvent(m, { id: "task-1", name: "task", phase: "start" });
    m = applyToolEvent(m, { id: "t1", name: "Read File", phase: "start", parentId: "task-1" });
    const startedAt = m.toolCalls?.find((c) => c.id === "t1")?.startedAt;
    vi.setSystemTime(5_000);
    m = applyToolEvent(m, { id: "t1", name: "Read app.py", phase: "start", input: "{}" });

    const card = m.toolCalls?.find((c) => c.id === "t1");
    expect(card?.startedAt).toBe(startedAt);
    expect(card?.parentId).toBe("task-1");
  });

  it("keeps the args when a re-announce carries none", () => {
    let m = bubble();
    m = applyToolEvent(m, { id: "t1", name: "current_time", phase: "start", input: '{"tz": "UTC"}' });
    m = applyToolEvent(m, { id: "t1", name: "current_time", phase: "start" });

    expect(m.toolCalls?.[0].input).toBe('{"tz": "UTC"}');
  });
});
