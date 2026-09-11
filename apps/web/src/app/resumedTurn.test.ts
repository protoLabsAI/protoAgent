import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import { resumedTurnRender, settleResumedTurn, streamedTextIsFinal } from "./resumedTurn";
import { applyProgressFrame, liveMessageId, type ProgressFrame } from "./serverTurnProgress";

describe("resumedTurnRender", () => {
  it("renders an ordinary resume as a completed answer", () => {
    const r = resumedTurnRender({ session_id: "chat-1", text: "Ship arrived; sold the ore.", task_id: "t9" });
    expect(r).not.toBeNull();
    expect(r!.failed).toBe(false);
    expect(r!.status).toBe("done");
    expect(r!.content).toBe("Ship arrived; sold the ore.");
    expect(r!.toast.tone).toBe("info");
  });

  it("marks a failed turn as an error and keeps the partial narration", () => {
    // The turn said things before it died — both halves matter: the narration is the only
    // record of what it did, the reason is the only record of why it stopped.
    const r = resumedTurnRender({
      session_id: "chat-1",
      text: "Grail Knight picks the ball up.",
      task_id: "t10",
      state: "failed",
      error: "Recursion limit of 200 reached without hitting a stop condition.",
    });
    expect(r!.failed).toBe(true);
    expect(r!.status).toBe("error");
    expect(r!.content).toContain("Grail Knight picks the ball up.");
    expect(r!.content).toContain("Recursion limit of 200");
    expect(r!.toast.tone).toBe("error");
    expect(r!.notify.title).toBe("Task failed");
  });

  it("renders a failure that produced no text at all", () => {
    // The case that used to vanish end to end: crashed before emitting anything, so the
    // old server guard dropped the event and the console showed no trace of the turn.
    const r = resumedTurnRender({
      session_id: "chat-1",
      text: "",
      task_id: "t11",
      state: "failed",
      error: "the model gateway refused the request",
    });
    expect(r).not.toBeNull();
    expect(r!.status).toBe("error");
    expect(r!.content).toBe("**Turn failed:** the model gateway refused the request");
    expect(r!.notify.body).toContain("gateway refused");
  });

  it("drops a genuinely empty event", () => {
    // Restraint control, paired with the case above so the two discriminate: widening the
    // guard to admit text-less FAILURES must not admit an empty bubble for anything else.
    expect(resumedTurnRender({ session_id: "chat-1", text: "", state: "completed" })).toBeNull();
    expect(resumedTurnRender({ session_id: "chat-1", text: "", state: "failed", error: "" })).toBeNull();
    expect(resumedTurnRender({ session_id: "", text: "hi" })).toBeNull();
  });

  it("keys off task id, falling back to session+prefix so a replay is idempotent", () => {
    expect(resumedTurnRender({ session_id: "chat-1", text: "hi", task_id: "t1" })!.key).toBe("t1");
    // No task id: a failure with no text still needs a stable key, and its error supplies one.
    expect(resumedTurnRender({ session_id: "chat-1", text: "", state: "failed", error: "boom" })!.key).toBe("chat-1:boom");
  });

  it("treats a missing OR empty state as completed, not as a failure", () => {
    // The direction of this fallback matters: an unknown state must not manufacture a
    // "Turn failed" on a turn that went fine. Both shapes reach the same answer, and the
    // server resolves it identically (`str(... or "completed")`).
    for (const state of [undefined, "", null]) {
      const r = resumedTurnRender({ session_id: "chat-1", text: "all good", state });
      expect(r!.failed, `state=${JSON.stringify(state)}`).toBe(false);
      expect(r!.status).toBe("done");
      expect(r!.content).toBe("all good");
    }
  });

  it("treats any non-completed state as failed", () => {
    // `canceled` is terminal-but-not-successful too, and the executor reports it the same way.
    const r = resumedTurnRender({ session_id: "chat-1", text: "partial", state: "canceled", error: "operator cancelled" });
    expect(r!.failed).toBe(true);
    expect(r!.status).toBe("error");
  });

  it("carries the trigger origin so the settled message renders as a compact result card (#3028)", () => {
    // ChatResumeWatch tags the assistant message with this origin; ChatMessageView then renders a
    // compact, expandable ServerResultCard instead of a full-size bubble.
    expect(resumedTurnRender({ session_id: "chat-1", text: "hi", origin: "scheduler" })!.origin).toBe("scheduler");
    expect(resumedTurnRender({ session_id: "chat-1", text: "hi", origin: "watch-42" })!.origin).toBe("watch-42");
    // A pre-#3028 server that never sets the field → "" (ChatResumeWatch falls back to the
    // origin the server-turn store captured at turn.started).
    expect(resumedTurnRender({ session_id: "chat-1", text: "hi" })!.origin).toBe("");
  });
});

describe("settleResumedTurn", () => {
  const LIVE = liveMessageId("task-9", "chat-1");
  const render = (text: string, state = "completed") =>
    resumedTurnRender({ session_id: "chat-1", task_id: "task-9", text, state, origin: "background-resume" })!;
  const frame = (kind: "text" | "steer", value: string): ProgressFrame =>
    kind === "text"
      ? { session: "chat-1", taskId: "task-9", kind, text: value }
      : { session: "chat-1", taskId: "task-9", kind, items: [{ id: "i1", text: value }] };

  it("replaces an un-split preview in place, keeping its tool cards (unchanged behavior)", () => {
    const live: ChatMessage = {
      id: LIVE,
      role: "assistant",
      content: "partial",
      parts: [{ kind: "text", text: "partial" }],
      toolCalls: [{ id: "t1", name: "read", input: "", output: "", status: "done" }],
      status: "streaming",
      createdAt: 5,
    };
    const out = settleResumedTurn([live], render("Whole answer."), "background-resume", "unused");
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      id: LIVE,
      content: "Whole answer.",
      status: "done",
      origin: "background-resume",
      createdAt: 5,
      taskId: "task-9",
    });
    expect(out[0].parts).toBeUndefined();
    expect(out[0].toolCalls).toHaveLength(1);
  });

  it("appends when the turn never previewed", () => {
    const out = settleResumedTurn([], render("Answer."), "scheduler", "new-id");
    expect(out.map((m) => [m.id, m.content, m.origin])).toEqual([["new-id", "Answer.", "scheduler"]]);
  });

  it("an interjection-split turn keeps the operator's message between the halves, text landed once", () => {
    // What the bus built: said-before, the operator's interjection, said-after.
    let msgs = applyProgressFrame([], frame("text", "Checked the PR."));
    msgs = applyProgressFrame(msgs, frame("steer", "yes 2024 as proposed"));
    msgs = applyProgressFrame(msgs, frame("text", "Locked it in."));

    // The terminal text is the WHOLE turn — replacing the continuation with it wholesale
    // printed "Checked the PR." twice.
    const out = settleResumedTurn(msgs, render("Checked the PR.\n\nLocked it in."), "background-resume", "unused");
    expect(out.map((m) => [m.role, m.content, m.status, m.origin])).toEqual([
      ["assistant", "Checked the PR.", "done", "background-resume"],
      ["user", "yes 2024 as proposed", "done", undefined],
      ["assistant", "Locked it in.", "done", "background-resume"],
    ]);
    expect(out[2].id).toBe(LIVE);
  });

  it("folds an empty continuation away when the agent said everything before the interjection", () => {
    let msgs = applyProgressFrame([], frame("text", "All done."));
    msgs = applyProgressFrame(msgs, frame("steer", "thanks"));
    const out = settleResumedTurn(msgs, render("All done."), "background-resume", "unused");
    expect(out.map((m) => [m.role, m.content])).toEqual([
      ["assistant", "All done."],
      ["user", "thanks"],
    ]);
  });

  it("a failed split turn still reads as failed", () => {
    let msgs = applyProgressFrame([], frame("text", "Started."));
    msgs = applyProgressFrame(msgs, frame("steer", "go on"));
    const failed = resumedTurnRender({
      session_id: "chat-1",
      task_id: "task-9",
      text: "Started.",
      state: "failed",
      error: "gateway timeout",
    })!;
    const out = settleResumedTurn(msgs, failed, "background-resume", "unused");
    const last = out[out.length - 1];
    expect(last.status).toBe("error");
    expect(last.content).toContain("gateway timeout");
    expect(out.filter((m) => m.content.includes("Started.")).length).toBe(1);
  });
});

describe("streamedTextIsFinal — keep the live layout when the words didn't change", () => {
  const parts = [
    { kind: "text" as const, text: "Three dice and all Push Back." },
    { kind: "tools" as const, ids: ["tc1"] },
    { kind: "text" as const, text: "Ball secured at (8,17)." },
  ];

  it("matches when the durable answer only joins the streamed segments differently", () => {
    // The preview splits text at tool boundaries; the durable answer joins them with
    // paragraph breaks (#3210). Same words → the settled message keeps the live order.
    expect(streamedTextIsFinal(parts, "Three dice and all Push Back.\n\nBall secured at (8,17).")).toBe(true);
  });

  it("lets the authoritative content win when the words differ", () => {
    expect(streamedTextIsFinal(parts, "Turn complete: the ball is secured.")).toBe(false);
    // …including a failure note appended to the partial narration.
    expect(
      streamedTextIsFinal(parts, "Three dice and all Push Back. Ball secured at (8,17).\n\n---\n\n**Turn failed:** x"),
    ).toBe(false);
  });

  it("has nothing to keep without streamed text", () => {
    expect(streamedTextIsFinal(undefined, "anything")).toBe(false);
    expect(streamedTextIsFinal([], "anything")).toBe(false);
    expect(streamedTextIsFinal([{ kind: "tools", ids: ["tc1"] }], "")).toBe(false);
  });
});
