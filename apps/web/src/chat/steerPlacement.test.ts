import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import { placeConsumedSteers } from "./steerPlacement";

const assistant = (content: string): ChatMessage => ({
  id: "live",
  role: "assistant",
  content,
  createdAt: 1,
  status: "streaming",
});

describe("placeConsumedSteers", () => {
  it("splits live work at the consumption boundary and preserves FIFO", () => {
    const out = placeConsumedSteers(
      [assistant("before")],
      [
        { id: "s1", text: "first" },
        { id: "s2", text: "second" },
      ],
      { inlineAssistantId: "live", frozenId: "frozen", createdAt: 10 },
    );
    expect(out.map((message) => [message.id, message.role, message.content])).toEqual([
      ["frozen", "assistant", "before"],
      ["s1", "user", "first"],
      ["s2", "user", "second"],
      ["live", "assistant", ""],
    ]);
  });

  it("deduplicates a replay or polling race by steer id", () => {
    const settled: ChatMessage = { id: "s1", role: "user", content: "first", status: "done" };
    const input = [settled, assistant("after")];
    expect(
      placeConsumedSteers(input, [{ id: "s1", text: "first" }], {
        inlineAssistantId: "live",
        frozenId: "unused",
        createdAt: 10,
      }),
    ).toBe(input);
  });

  it("keeps the conservative before-assistant placement when no live marker was observed", () => {
    const original: ChatMessage = { id: "u", role: "user", content: "start", status: "done" };
    const out = placeConsumedSteers(
      [original, assistant("whole answer")],
      [{ id: "s1", text: "redirect" }],
      { frozenId: "unused", createdAt: 10 },
    );
    expect(out.map((message) => message.id)).toEqual(["u", "s1", "live"]);
  });

  it("does not resurrect a streaming placeholder when a late marker reaches a settled turn", () => {
    const done = { ...assistant("whole answer"), status: "done" as const };
    const out = placeConsumedSteers(
      [done],
      [{ id: "s1", text: "redirect" }],
      { inlineAssistantId: "live", frozenId: "unused", createdAt: 10 },
    );
    expect(out.map((message) => [message.id, message.status])).toEqual([
      ["s1", "done"],
      ["live", "done"],
    ]);
  });
});
