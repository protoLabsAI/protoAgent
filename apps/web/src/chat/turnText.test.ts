import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import { placeConsumedSteers } from "./steerPlacement";
import { applyText, applyToolEvent } from "./turnReducers";
import {
  applyCanonicalTurnText,
  markTurnAnsweredByParticipants,
  repairAddressedTurnEcho,
  repairDuplicatedTurnText,
  resetTurnForSnapshot,
  settleTurnBubbles,
  turnAnsweredByParticipants,
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

  it("un-split tool turn: the terminal replace keeps narration above AND below the card", () => {
    // Built from the frames the server streams for "narrate → tool → narrate": the
    // post-tool delta opens with the paragraph break the canonical text also carries.
    // The live terminal frame lands through here, and a byte compare against the text
    // the parts render ("…first." + "It is noon.") moved all prose below the card.
    let bubble = live();
    bubble = applyText(bubble, "I'll check the time first.", true);
    bubble = applyToolEvent(bubble, { id: "t1", name: "current_time", phase: "start" });
    bubble = applyToolEvent(bubble, { id: "t1", name: "current_time", phase: "end", output: "12:00" });
    bubble = applyText(bubble, "\n\nIt is noon.", true);
    const out = applyCanonicalTurnText([user("hi"), bubble], "A", "I'll check the time first.\n\nIt is noon.");
    expect(out[1].parts).toEqual([
      { kind: "text", text: "I'll check the time first." },
      { kind: "tools", ids: ["t1"] },
      { kind: "text", text: "It is noon." },
    ]);
    expect(out[1].content).toBe("I'll check the time first.\n\nIt is noon.");
  });

  it("un-split turn: the terminal replace heals a list whose line-break frame was lost", () => {
    // The live path. A lone "\n" frame lost en route leaves "- one- two" in the run; the
    // flat copy is repaired by the replace, and what the bubble RENDERS must be too.
    let bubble = live();
    bubble = applyText(bubble, "Steps:\n\n- one", true);
    bubble = applyText(bubble, "- two", true);
    const canonical = "Steps:\n\n- one\n- two";
    const [, settled] = applyCanonicalTurnText([user("list"), bubble], "A", canonical);
    expect(settled.content).toBe(canonical);
    expect(text(settled)).toBe(canonical);
  });

  it("split turn: an earlier half whose run lost a line break is not trusted as a prefix", () => {
    const messages = [
      user("hi"),
      frozen({ content: "Steps:\n- one- two", parts: [{ kind: "text", text: "Steps:\n- one- two" }] }),
      user("steer", "s"),
      live({ content: "Done.", parts: [{ kind: "text", text: "Done." }] }),
    ];
    const out = applyCanonicalTurnText(messages, "A", "Steps:\n- one\n- two\n\nDone.");
    // The broken half's prose goes; the whole answer lands once, correctly, below.
    expect(rendered(out)).toBe("Steps:\n- one\n- two\n\nDone.");
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


// ── an addressed turn's answer belongs to the participants (#3449) ───────────────
//
// The words are already on screen in the participants' own bubbles; the turn's canonical
// answer text merely restates them for clients that cannot render bubbles. FIVE producers
// land that text — the terminal replace, the 45s stranded-turn watchdog, the post-stream
// `!sawAuthoritativeText` reconcile, reattach (whose handler set has no `onRoomReply` at
// all), and ADR 0104 boot hydration — and every one of them comes through
// `applyCanonicalTurnText`. So the refusal lives HERE, off a fact stamped on the
// transcript, rather than in a ref that dies with the live stream.
describe("a turn answered by addressed participants", () => {
  /** The shape an `@name` turn settles into: the work card, frozen out of the turn when
   *  the participant's bubble was inserted, plus that authored bubble. The continuation
   *  was folded away at `done`. */
  const settled = (): ChatMessage[] => [
    user("@protoEngineer what version?"),
    frozen({ answeredByParticipants: true, parts: [{ kind: "tools", ids: ["mention:protoEngineer"] }] }),
    {
      id: "P",
      role: "assistant",
      content: "0.17.0, in-tree at plugins/artifact/.",
      createdAt: 2,
      status: "done",
      author: { name: "protoEngineer" },
      taskId: TASK,
    },
  ];

  it("reads the stamp from whichever bubble of the turn survived", () => {
    expect(turnAnsweredByParticipants(settled(), "A")).toBe(true);
    // Findable from the frozen half's own id too — the continuation it names is gone.
    expect(turnAnsweredByParticipants(settled(), "F")).toBe(true);
    expect(turnAnsweredByParticipants([user("hi"), live()], "A")).toBe(false);
  });

  it("stamps every bubble of the turn, so the one that survives the settle carries it", () => {
    const marked = markTurnAnsweredByParticipants([user("@x hi"), frozen({ content: "" }), live()], "A");
    expect(marked.filter((m) => m.answeredByParticipants).map((m) => m.id)).toEqual(["F", "A"]);
    // …and only this turn's: an unrelated earlier turn is untouched.
    const other: ChatMessage = { id: "Z", role: "assistant", content: "earlier", createdAt: 0, status: "done", taskId: "task-0" };
    expect(markTurnAnsweredByParticipants([other, live()], "A").find((m) => m.id === "Z")?.answeredByParticipants).toBeUndefined();
  });

  it("lands NOTHING — the canonical answer is a restatement of what is already shown", () => {
    const messages = settled();
    const canonical = "0.17.0, in-tree at plugins/artifact/.";
    expect(applyCanonicalTurnText(messages, "A", canonical)).toBe(messages); // same ref: no rewrite
    expect(rendered(applyCanonicalTurnText(messages, "A", canonical)).split("0.17.0").length - 1).toBe(1);
  });

  it("refuses a MULTI-address answer, which no text compare could ever match", () => {
    // The join attributes each reply (`**@proto** — line 40`), so the bubbles are not a
    // prefix of it and `renderedPrefixEnd` returns -1 — the diverged path, which lands
    // the whole thing. Only the stamp can answer this one.
    const messages: ChatMessage[] = [
      user("@proto @reviewer status?"),
      frozen({ answeredByParticipants: true, parts: [{ kind: "tools", ids: ["mention:proto,reviewer"] }] }),
      { id: "P1", role: "assistant", content: "line 40", createdAt: 2, status: "done", author: { name: "proto" } },
      { id: "P2", role: "assistant", content: "agreed", createdAt: 3, status: "done", author: { name: "reviewer" } },
    ];
    const canonical = "**@proto** — line 40\n\n**@reviewer** — agreed";
    expect(applyCanonicalTurnText(messages, "A", canonical)).toBe(messages);
    expect(rendered(messages)).toBe("|line 40|agreed");
  });

  it("still lands on an UNSTAMPED turn that merely has an authored bubble near it", () => {
    // A `delegate_to`-moderated turn: the participant's reply is a bubble, but the lead
    // RAN and the canonical text is its own synthesis. Nothing is stamped, so it lands —
    // refusing here would delete the lead's answer.
    const messages: ChatMessage[] = [
      user("have proto look at auth"),
      frozen({ content: "I'll ask proto.", parts: [{ kind: "text", text: "I'll ask proto." }] }),
      { id: "P", role: "assistant", content: "patched", createdAt: 2, status: "done", author: { name: "proto" } },
      live(),
    ];
    const out = applyCanonicalTurnText(messages, "A", "I'll ask proto.\n\nproto handled it.");
    expect(rendered(out)).toBe("I'll ask proto.|patched|proto handled it.");
  });
});


// The one-time repair for what v0.164.0 already wrote to disk (#3449).
describe("repairAddressedTurnEcho", () => {
  const ANSWER = "0.17.0, in-tree at plugins/artifact/.";
  /** Exactly what the released console persisted for `@protoEngineer what version?`. */
  const releasedShape = (): ChatMessage[] => [
    user("@protoEngineer what version?"),
    frozen({ parts: [{ kind: "tools", ids: ["mention:protoEngineer"] }] }),
    { id: "P", role: "assistant", content: ANSWER, createdAt: 2, status: "done", author: { name: "protoEngineer" } },
    live({ status: "done", content: ANSWER, parts: [{ kind: "text", text: ANSWER }] }),
  ];

  it("removes the unattributed second copy and stamps the turn", () => {
    const out = repairAddressedTurnEcho(releasedShape());
    expect(rendered(out)).toBe(`|${ANSWER}`); // the card half, then the reply — once
    expect(out.map((m) => m.id)).toEqual(["u", "F", "P"]); // the echo carried nothing else, so it goes
    // …and the turn is stamped, so hydration cannot put it back on the next boot.
    expect(turnAnsweredByParticipants(out, "F")).toBe(true);
  });

  it("keeps a bubble that still has work to show, minus the duplicated prose", () => {
    const withCards = releasedShape();
    withCards[3] = live({
      status: "done",
      content: ANSWER,
      parts: [{ kind: "text", text: ANSWER }, { kind: "tools", ids: ["t1"] }],
      toolCalls: [{ id: "t1", name: "read_file", status: "done" }],
    });
    const out = repairAddressedTurnEcho(withCards);
    expect(out).toHaveLength(4);
    expect(out[3].toolCalls).toHaveLength(1); // the record of what the turn did stays
    expect(rendered(out)).toBe(`|${ANSWER}|`);
  });

  it("leaves a moderated delegation alone — the lead's answer is its own", () => {
    // `delegate_to`: the participant's reply, then the lead's SYNTHESIS. Different
    // words, so nothing is on screen twice and nothing is touched.
    const moderated: ChatMessage[] = [
      user("have proto look at auth"),
      { id: "P", role: "assistant", content: "patched", createdAt: 2, status: "done", author: { name: "proto" } },
      live({ status: "done", content: "proto handled it.", parts: [{ kind: "text", text: "proto handled it." }] }),
    ];
    expect(repairAddressedTurnEcho(moderated)).toBe(moderated);
  });

  it("is inert on a transcript the fixed code wrote", () => {
    const fixed = releasedShape().map((m) =>
      m.role === "assistant" && !m.author ? { ...m, answeredByParticipants: true } : m,
    );
    expect(repairAddressedTurnEcho(fixed)).toBe(fixed);
  });

  it("leaves a mere PREFIX match alone — only an exact echo is proof", () => {
    const partial = releasedShape();
    partial[3] = live({
      status: "done",
      content: `${ANSWER} I also checked the changelog.`,
      parts: [{ kind: "text", text: `${ANSWER} I also checked the changelog.` }],
    });
    expect(repairAddressedTurnEcho(partial)).toBe(partial);
  });
});
