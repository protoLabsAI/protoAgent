import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import { placeConsumedSteers, placeServerTurnSteers } from "./steerPlacement";

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

describe("placeServerTurnSteers", () => {
  const LIVE = "server-turn-task-9";
  const prior: ChatMessage[] = [
    { id: "u0", role: "user", content: "Which date?", status: "done" },
    { id: "a0", role: "assistant", content: "Dec 2024, as proposed?", status: "done" },
  ];
  const preview = (content: string, status: ChatMessage["status"] = "streaming"): ChatMessage => ({
    id: LIVE,
    role: "assistant",
    content,
    parts: [{ kind: "text", text: content }],
    status,
    taskId: "task-9",
  });
  const item = { id: "i1", text: "yes 2024 as proposed" };

  it("splits the live server-turn preview at the reported boundary", () => {
    const out = placeServerTurnSteers([...prior, preview("Checked the PR.")], [item], {
      liveId: LIVE,
      exact: true,
      frozenId: "frozen",
      createdAt: 10,
    });
    expect(out.map((message) => [message.id, message.role, message.content, message.splitOf])).toEqual([
      ["u0", "user", "Which date?", undefined],
      ["a0", "assistant", "Dec 2024, as proposed?", undefined],
      ["frozen", "assistant", "Checked the PR.", LIVE],
      ["i1", "user", "yes 2024 as proposed", undefined],
      [LIVE, "assistant", "", undefined],
    ]);
  });

  it("with no preview yet, lands as the newest row — never above the PREVIOUS turn's answer", () => {
    // placeConsumedSteers' fallback would put it before the last assistant message, which
    // here is the prior turn's answer: the operator's reply would jump above the question.
    const out = placeServerTurnSteers(prior, [item], { liveId: LIVE, exact: true, frozenId: "unused", createdAt: 10 });
    expect(out.map((message) => message.id)).toEqual(["u0", "a0", "i1"]);
  });

  it("a missed marker (turn-end reconcile) lands above the turn's reply instead of guessing a split", () => {
    const out = placeServerTurnSteers([...prior, preview("Checked the PR. Locked it in.")], [item], {
      liveId: LIVE,
      exact: false,
      frozenId: "unused",
      createdAt: 10,
    });
    expect(out.map((message) => message.id)).toEqual(["u0", "a0", "i1", LIVE]);
    expect(out.find((message) => message.id === LIVE)?.content).toBe("Checked the PR. Locked it in.");
  });

  it("anchors a missed marker above the FIRST bubble of a turn another interjection already split", () => {
    const split: ChatMessage[] = [
      ...prior,
      { ...preview("Checked the PR.", "done"), id: "frozen", splitOf: LIVE },
      { id: "i0", role: "user", content: "earlier interjection", status: "done" },
      preview("Locked it in.", "done"),
    ];
    const out = placeServerTurnSteers(split, [item], { liveId: LIVE, exact: false, frozenId: "unused", createdAt: 10 });
    expect(out.map((message) => message.id)).toEqual(["u0", "a0", "i1", "frozen", "i0", LIVE]);
  });

  it("a late exact marker for an already-settled turn does not reopen it", () => {
    const out = placeServerTurnSteers([...prior, preview("Whole answer.", "done")], [item], {
      liveId: LIVE,
      exact: true,
      frozenId: "unused",
      createdAt: 10,
    });
    expect(out.map((message) => [message.id, message.status])).toEqual([
      ["u0", "done"],
      ["a0", "done"],
      ["i1", "done"],
      [LIVE, "done"],
    ]);
  });

  it("is idempotent by interjection id", () => {
    const once = placeServerTurnSteers([...prior, preview("Checked.")], [item], {
      liveId: LIVE,
      exact: true,
      frozenId: "frozen",
      createdAt: 10,
    });
    expect(placeServerTurnSteers(once, [item], { liveId: LIVE, exact: true, frozenId: "again", createdAt: 11 })).toBe(once);
  });
});
