import { describe, expect, it } from "vitest";

import { resumedTurnRender } from "./resumedTurn";

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
});
