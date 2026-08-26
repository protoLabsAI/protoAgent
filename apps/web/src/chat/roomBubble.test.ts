import { describe, expect, it } from "vitest";

import { insertConversationBubbles, insertRoomBubble, isEmptyPlaceholder } from "./roomBubble";
import type { ChatMessage } from "../lib/types";

const ph = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "A",
  role: "assistant",
  content: "",
  createdAt: 1,
  status: "streaming",
  ...over,
});
const bubble = (name: string): ChatMessage => ({
  id: "b",
  role: "assistant",
  content: `${name} says hi`,
  author: { name },
  createdAt: 2,
  status: "done",
});

describe("insertRoomBubble", () => {
  it("empty placeholder: bubble goes BEFORE it (fan-out / pre-work delegation)", () => {
    const out = insertRoomBubble([ph()], "A", bubble("proto"), "F");
    expect(out.map((m) => [m.id, m.author?.name])).toEqual([
      ["b", "proto"],
      ["A", undefined],
    ]);
  });

  it("non-empty placeholder: SPLITS — frozen lead work, then bubble, then fresh placeholder", () => {
    // The reported bug: the lead streamed "I'll coordinate" + a tool card, THEN delegated.
    const working = ph({ content: "I'll coordinate", toolCalls: [{ id: "t", name: "list_agents" } as never] });
    const out = insertRoomBubble([working], "A", bubble("proto"), "F");
    expect(out.map((m) => m.id)).toEqual(["F", "b", "A"]);
    // Frozen lead work keeps its content, is done, new id:
    expect(out[0]).toMatchObject({ id: "F", content: "I'll coordinate", status: "done" });
    // Bubble in the middle — AFTER the work, not above it:
    expect(out[1].author?.name).toBe("proto");
    // Fresh placeholder keeps the streaming id, emptied for continued streaming:
    expect(out[2]).toMatchObject({ id: "A", content: "", status: "streaming" });
  });

  it("preserves messages before the placeholder (a prior round's bubbles)", () => {
    const prior = bubble("reviewer");
    const out = insertRoomBubble([prior, ph({ content: "next" })], "A", bubble("proto"), "F");
    expect(out.map((m) => m.id)).toEqual(["b", "F", "b", "A"]);
  });

  it("no placeholder in the array: append", () => {
    const out = insertRoomBubble([bubble("x")], "MISSING", bubble("proto"), "F");
    expect(out.map((m) => m.id)).toEqual(["b", "b"]);
  });
});

describe("isEmptyPlaceholder", () => {
  it("true for a bare streaming placeholder, false once anything streams", () => {
    expect(isEmptyPlaceholder(ph())).toBe(true);
    expect(isEmptyPlaceholder(ph({ content: "x" }))).toBe(false);
    expect(isEmptyPlaceholder(ph({ toolCalls: [{ id: "t" } as never] }))).toBe(false);
    expect(isEmptyPlaceholder(ph({ parts: [{ kind: "text" } as never] }))).toBe(false);
    expect(isEmptyPlaceholder(undefined)).toBe(true);
  });
});

describe("insertConversationBubbles", () => {
  it("inserts a FIFO batch of consumed user steers at one live boundary", () => {
    const steers: ChatMessage[] = [
      { id: "s1", role: "user", content: "first redirect", createdAt: 2, status: "done" },
      { id: "s2", role: "user", content: "then this", createdAt: 3, status: "done" },
    ];
    const out = insertConversationBubbles([ph({ content: "work before" })], "A", steers, "F");

    expect(out.map((message) => message.id)).toEqual(["F", "s1", "s2", "A"]);
    expect(out.map((message) => message.role)).toEqual(["assistant", "user", "user", "assistant"]);
    expect(out[0]).toMatchObject({ content: "work before", status: "done" });
    expect(out[3]).toMatchObject({ content: "", status: "streaming" });
  });

  it("puts a pre-work steer before an empty placeholder without minting a frozen bubble", () => {
    const steer: ChatMessage = { id: "s1", role: "user", content: "redirect", status: "done" };
    const out = insertConversationBubbles([ph()], "A", [steer], "unused");
    expect(out.map((message) => message.id)).toEqual(["s1", "A"]);
  });

  it("preserves the live task id on the reset placeholder for reload recovery", () => {
    const steer: ChatMessage = { id: "s1", role: "user", content: "redirect", status: "done" };
    const out = insertConversationBubbles(
      [ph({ content: "work before", taskId: "task-123" })],
      "A",
      [steer],
      "F",
    );
    expect(out[out.length - 1]).toMatchObject({ id: "A", status: "streaming", taskId: "task-123" });
  });

  it("is a no-op for an empty batch", () => {
    const input = [ph({ content: "work" })];
    expect(insertConversationBubbles(input, "A", [], "F")).toBe(input);
  });
});
