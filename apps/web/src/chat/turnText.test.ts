import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import { placeConsumedSteers } from "./steerPlacement";
import { applyText } from "./turnReducers";
import {
  applyCanonicalTurnText,
  canonicalRemainderIndex,
  repairDuplicatedTurnText,
  resetTurnForSnapshot,
  settleTurnBubbles,
  turnBubbleIndexes,
} from "./turnText";

const TASK = "task-1";

const live = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "A",
  role: "assistant",
  content: "",
  createdAt: 1,
  status: "streaming",
  taskId: TASK,
  ...over,
});
/** A half frozen out of the live turn by an inline split. */
const frozen = (over: Partial<ChatMessage> = {}): ChatMessage =>
  live({ id: "F", status: "done", splitOf: "A", ...over });
const user = (content: string, id = "u"): ChatMessage => ({
  id,
  role: "user",
  content,
  createdAt: 1,
  status: "done",
});
const text = (message: ChatMessage): string =>
  (message.parts ?? [])
    .filter((part) => part.kind === "text")
    .map((part) => part.text)
    .join("");
/** Every copy of the turn's prose the transcript would render, joined. */
const rendered = (messages: ChatMessage[]): string =>
  messages
    .filter((message) => message.role === "assistant")
    .map((message) => (message.parts?.length ? text(message) : message.content))
    .join("|");

describe("canonicalRemainderIndex", () => {
  it("returns where the canonical answer continues past what is already shown", () => {
    expect(canonicalRemainderIndex("one two three", "one two")).toBe(7);
    // The index lands just past the last matched character; the caller trims the
    // whitespace that separated it from what follows.
    expect("one two three".slice(7).trimStart()).toBe("three");
  });

  it("tolerates whitespace the two sides disagree about (the #3210 separator)", () => {
    // Server injects a blank line between pre- and post-tool narration; the client's
    // own delta accumulation never had one.
    expect(canonicalRemainderIndex("Checking.\n\nHere it is.", "Checking.")).toBe(9);
    expect(canonicalRemainderIndex("  Checking. Here", "Checking.")).toBe(11);
  });

  it("is -1 when the shown text is not a prefix at all", () => {
    expect(canonicalRemainderIndex("a totally different answer", "Checking.")).toBe(-1);
    expect(canonicalRemainderIndex("short", "a much longer prefix")).toBe(-1);
  });

  it("consumes the whole canonical answer when nothing followed", () => {
    expect(canonicalRemainderIndex("all of it", "all of it")).toBe(9);
    expect("all of it".slice(9)).toBe("");
  });
});

describe("turnBubbleIndexes", () => {
  it("is just the anchor for an ordinary un-split turn", () => {
    expect(turnBubbleIndexes([user("hi"), live({ content: "hello" })], "A")).toEqual([1]);
  });

  it("groups the halves a split left behind, in transcript order", () => {
    const messages = [user("hi"), frozen({ content: "part one" }), user("wait", "s"), live()];
    expect(turnBubbleIndexes(messages, "A")).toEqual([1, 3]);
  });

  it("resolves the same turn from a frozen half's id", () => {
    const messages = [frozen({ content: "part one" }), live()];
    expect(turnBubbleIndexes(messages, "F")).toEqual([0, 1]);
  });

  it("groups every half when one turn was split more than once", () => {
    const messages = [
      frozen({ id: "F1", content: "one" }),
      user("steer", "s1"),
      frozen({ id: "F2", content: "two" }),
      user("steer again", "s2"),
      live({ content: "three" }),
    ];
    expect(turnBubbleIndexes(messages, "A")).toEqual([0, 2, 4]);
  });

  it("never reaches across turns that merely share a task id (a HITL resume)", () => {
    const messages = [
      live({ id: "EARLIER", content: "before the pause", status: "done" }),
      user("staging", "answer"),
      live({ content: "after the resume" }),
    ];
    expect(turnBubbleIndexes(messages, "A")).toEqual([2]);
  });

  it("excludes a participant's own reply, which is not the lead's answer", () => {
    const messages = [
      frozen({ content: "part one" }),
      { ...live({ id: "R", content: "delegate says hi", status: "done" }), author: { name: "proto" } },
      live(),
    ];
    expect(turnBubbleIndexes(messages, "A")).toEqual([0, 2]);
  });

  it("still finds the turn after its continuation was folded away", () => {
    // settleTurnBubbles removes a spent continuation, but callers still hold its id and
    // may have more to reconcile. The surviving half names it, so the turn stays findable.
    const messages = [user("hi"), frozen({ content: "the answer" }), user("steer", "s")];
    expect(turnBubbleIndexes(messages, "A")).toEqual([1]);
  });

  it("is empty when the anchor is gone (a cleared transcript)", () => {
    expect(turnBubbleIndexes([user("hi")], "A")).toEqual([]);
  });
});

describe("applyCanonicalTurnText", () => {
  it("un-split turn: same as the per-message replace it replaces", () => {
    const messages = [user("hi"), live({ content: "partial" })];
    const out = applyCanonicalTurnText(messages, "A", "the whole answer");
    expect(out[1]).toEqual(applyText(messages[1], "the whole answer", false));
  });

  it("split turn: the continuation takes only what followed the split", () => {
    const messages = [
      user("hi"),
      frozen({ content: "Before the steer.", parts: [{ kind: "text", text: "Before the steer." }] }),
      user("actually…", "s"),
      live({ content: "After it.", parts: [{ kind: "text", text: "After it." }] }),
    ];
    const out = applyCanonicalTurnText(messages, "A", "Before the steer.\n\nAfter it.");
    expect(out[1].content).toBe("Before the steer.");
    expect(out[3].content).toBe("After it.");
    expect(rendered(out)).toBe("Before the steer.|After it.");
  });

  it("THE BUG: a terminal full-turn replace never re-lands the frozen prose (#3387)", () => {
    // The exact live shape: the agent had said everything BEFORE the steer landed,
    // so the continuation is still empty when the canonical answer arrives.
    const messages = [
      user("hi"),
      frozen({ content: "The whole answer.", parts: [{ kind: "text", text: "The whole answer." }] }),
      user("one more thing", "s"),
      live({ parts: [] }),
    ];
    const out = applyCanonicalTurnText(messages, "A", "The whole answer.");
    expect(rendered(out)).toBe("The whole answer.|");
    expect(out[3].content).toBe("");
  });

  it("keeps the bubble's own status — a canonical replace never resurrects a settled turn", () => {
    const messages = [live({ content: "done already", status: "done" })];
    expect(applyCanonicalTurnText(messages, "A", "canonical")[0].status).toBe("done");
    expect(applyCanonicalTurnText([live()], "A", "canonical")[0].status).toBe("streaming");
  });

  it("divergence: strips the earlier bubbles' text and lands the answer once, cards kept", () => {
    const messages = [
      frozen({
        content: "a lost-chunk accumulation",
        parts: [{ kind: "text", text: "a lost-chunk accumulation" }, { kind: "tools", ids: ["t"] }],
      }),
      user("steer", "s"),
      live({ content: "tail", parts: [{ kind: "text", text: "tail" }] }),
    ];
    const out = applyCanonicalTurnText(messages, "A", "an entirely different canonical answer");
    expect(rendered(out)).toBe("|an entirely different canonical answer");
    expect(out[0].parts).toEqual([{ kind: "tools", ids: ["t"] }]);
  });

  it("divergence: drops an earlier half left with nothing, rather than a blank row", () => {
    const messages = [
      frozen({ content: "prose only", parts: [{ kind: "text", text: "prose only" }] }),
      user("steer", "s"),
      live({ content: "tail", parts: [{ kind: "text", text: "tail" }] }),
    ];
    const out = applyCanonicalTurnText(messages, "A", "an entirely different answer");
    expect(out.map((m) => m.id)).toEqual(["s", "A"]);
    expect(rendered(out)).toBe("an entirely different answer");
  });

  it("is a no-op when the anchor is gone", () => {
    const messages = [user("hi")];
    expect(applyCanonicalTurnText(messages, "A", "x")).toBe(messages);
  });
});

describe("settleTurnBubbles", () => {
  it("folds away a continuation the canonical text left empty, keeping its footer", () => {
    const messages = [
      frozen({ content: "the answer" }),
      user("steer", "s"),
      live({ status: "done", usage: { costUsd: 0.02 } as never }),
    ];
    const out = settleTurnBubbles(messages, "A");
    expect(out.map((m) => m.id)).toEqual(["F", "s"]);
    expect(out[0].usage).toEqual({ costUsd: 0.02 });
  });

  it("carries a FAILED continuation's status to the half it folds into", () => {
    // Otherwise a failed turn renders as a clean answer: the frozen half was stamped
    // "done" at split time and never learns the turn went wrong.
    const messages = [
      frozen({ content: "the answer" }),
      user("steer", "s"),
      live({ status: "error" }),
    ];
    const out = settleTurnBubbles(messages, "A");
    expect(out.map((m) => m.id)).toEqual(["F", "s"]);
    expect(out[0].status).toBe("error");
  });

  it("keeps a continuation that carries anything of its own", () => {
    const withText = [frozen({ content: "one" }), live({ content: "two", status: "done" })];
    expect(settleTurnBubbles(withText, "A")).toBe(withText);
    const withCard = [
      frozen({ content: "one" }),
      live({ status: "done", toolCalls: [{ id: "t", name: "read_file" } as never] }),
    ];
    expect(settleTurnBubbles(withCard, "A")).toBe(withCard);
  });

  it("never touches an un-split turn", () => {
    const messages = [live({ status: "done" })];
    expect(settleTurnBubbles(messages, "A")).toBe(messages);
  });
});

describe("resetTurnForSnapshot", () => {
  it("folds a split turn back to its anchor, leaving the inserted rows in place", () => {
    const messages = [
      user("hi"),
      frozen({ content: "part one" }),
      user("steer", "s"),
      live({ content: "part two" }),
    ];
    expect(resetTurnForSnapshot(messages, "A").map((m) => m.id)).toEqual(["u", "s", "A"]);
  });

  it("leaves an un-split turn alone", () => {
    const messages = [user("hi"), live()];
    expect(resetTurnForSnapshot(messages, "A")).toBe(messages);
  });
});

describe("repairDuplicatedTurnText", () => {
  it("strips the prose a pre-fix transcript persisted twice", () => {
    // Written by the old path: the continuation was handed the WHOLE turn.
    const messages = [
      user("hi"),
      live({ id: "F", content: "Before.", parts: [{ kind: "text", text: "Before." }], status: "done" }),
      user("steer", "s"),
      live({ content: "Before.\n\nAfter.", parts: [{ kind: "text", text: "Before.\n\nAfter." }], status: "done" }),
    ];
    const out = repairDuplicatedTurnText(messages);
    expect(rendered(out)).toBe("Before.|After.");
  });

  it("drops a bubble the repair empties, so no blank row is left behind", () => {
    const messages = [
      live({ id: "F", content: "All of it.", parts: [{ kind: "text", text: "All of it." }], status: "done" }),
      user("steer", "s"),
      live({ content: "All of it.", parts: [{ kind: "text", text: "All of it." }], status: "done" }),
    ];
    expect(repairDuplicatedTurnText(messages).map((m) => m.id)).toEqual(["F", "s"]);
  });

  it("keeps a duplicated bubble's tool cards when its text goes", () => {
    const messages = [
      live({ id: "F", content: "All of it.", parts: [{ kind: "text", text: "All of it." }], status: "done" }),
      live({
        content: "All of it.",
        parts: [{ kind: "text", text: "All of it." }, { kind: "tools", ids: ["t"] }],
        toolCalls: [{ id: "t", name: "read_file" } as never],
        status: "done",
      }),
    ];
    const out = repairDuplicatedTurnText(messages);
    expect(out).toHaveLength(2);
    expect(out[1].parts).toEqual([{ kind: "tools", ids: ["t"] }]);
  });

  it("leaves healthy transcripts untouched (identity, so the store skips the write)", () => {
    const split = [
      live({ id: "F", content: "Before.", status: "done" }),
      live({ content: "After.", status: "done" }),
    ];
    expect(repairDuplicatedTurnText(split)).toBe(split);
    const ordinary = [user("hi"), live({ content: "one answer", status: "done" })];
    expect(repairDuplicatedTurnText(ordinary)).toBe(ordinary);
  });

  it("never crosses turns — two turns that happen to start alike are left alone", () => {
    const messages = [
      live({ id: "A1", taskId: "t1", content: "Sure. Here you go.", status: "done" }),
      live({ id: "A2", taskId: "t2", content: "Sure. Here you go. And more.", status: "done" }),
    ];
    expect(repairDuplicatedTurnText(messages)).toBe(messages);
  });

  it("leaves same-task bubbles alone unless one wholly repeats the other", () => {
    // Everything this repair has to go on is the task id, so pin what it does with
    // several bubbles in one task that are NOT a duplication: nothing.
    const messages = [
      live({ id: "A1", content: "Here is the plan.", status: "done" }),
      user("staging", "answer"),
      live({ id: "A2", content: "Deploying to staging now.", status: "done" }),
    ];
    expect(repairDuplicatedTurnText(messages)).toBe(messages);
  });

  it("acts on a same-task duplicate whatever seam produced it — the prose IS on screen twice", () => {
    // The condition is its own proof: the later bubble renders the whole of the
    // earlier one's text before adding its own. See the function's docstring for why
    // that differs from turnBubbleIndexes' refusal to group by task.
    const messages = [
      live({ id: "A1", content: "Here is the plan.", status: "done" }),
      user("staging", "answer"),
      live({ id: "A2", content: "Here is the plan. Deploying now.", status: "done" }),
    ];
    expect(repairDuplicatedTurnText(messages).map((m) => m.content)).toEqual([
      "Here is the plan.",
      "staging",
      "Deploying now.",
    ]);
  });

  it("skips a turn the FIXED path wrote — `splitOf` present means it is already correct", () => {
    // Self-limiting: once history has turned over, this migration is inert. Without
    // the guard the repair would keep re-inspecting transcripts it can only harm.
    const messages = [
      frozen({ content: "Before." }),
      user("steer", "s"),
      live({ content: "Before. After.", status: "done" }),
    ];
    expect(repairDuplicatedTurnText(messages)).toBe(messages);
  });
});

describe("the split → terminal replace round trip", () => {
  it("renders the answer exactly once, steer inline (the #3387 regression)", () => {
    // 1. The turn streams its preamble and a tool card.
    let messages: ChatMessage[] = [user("do the thing")];
    messages.push(live({ content: "Working on it.", parts: [{ kind: "text", text: "Working on it." }] }));
    // 2. The operator interjects; the agent folds it in and the console splits.
    messages = placeConsumedSteers(messages, [{ id: "s", text: "actually, also…" }], {
      inlineAssistantId: "A",
      frozenId: "F",
      createdAt: 2,
    });
    expect(messages.map((m) => m.id)).toEqual(["u", "F", "s", "A"]);
    // 3. The agent continues after the steer.
    messages = messages.map((m) => (m.id === "A" ? applyText(m, "Here you go.", true) : m));
    // 4. The terminal frame re-sends the WHOLE turn (#1717).
    messages = applyCanonicalTurnText(messages, "A", "Working on it.\n\nHere you go.");
    messages = settleTurnBubbles(messages, "A");

    expect(messages.map((m) => m.id)).toEqual(["u", "F", "s", "A"]);
    expect(rendered(messages)).toBe("Working on it.|Here you go.");
    const answer = messages.filter((m) => m.role === "assistant").map((m) => m.content).join("\n");
    expect(answer.match(/Working on it\./g)).toHaveLength(1);
  });
});
