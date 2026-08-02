import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import {
  applyProgressFrame,
  isLiveServerTurn,
  liveMessageId,
  parseProgress,
  type ProgressFrame,
} from "./serverTurnProgress";

const text = (t: string, taskId = "task-1"): ProgressFrame => ({
  session: "s1",
  taskId,
  kind: "text",
  text: t,
});
const tool = (
  id: string,
  name: string,
  done = false,
  output = "",
  error = false,
  taskId = "task-1",
): ProgressFrame => ({ session: "s1", taskId, kind: "tool", done, id, name, output, error });

describe("parseProgress", () => {
  it("reads a text frame", () => {
    expect(parseProgress({ session_id: "s1", task_id: "t", phase: "text", text: "hi" })).toEqual({
      session: "s1",
      taskId: "t",
      kind: "text",
      text: "hi",
    });
  });

  it("reads a tool frame and marks tool_end done", () => {
    const f = parseProgress({
      session_id: "s1",
      task_id: "t",
      phase: "tool_end",
      tool: "roll_block",
      tool_call_id: "tc1",
      output: "3 dice",
    });
    expect(f).toMatchObject({ kind: "tool", done: true, id: "tc1", name: "roll_block", output: "3 dice" });
  });

  it("drops a frame with no session — there is no bubble to grow", () => {
    expect(parseProgress({ task_id: "t", phase: "text", text: "hi" })).toBeNull();
  });

  it("drops empty text rather than appending nothing", () => {
    expect(parseProgress({ session_id: "s1", phase: "text", text: "" })).toBeNull();
  });

  it("drops a tool frame with no id — it could not be correlated start→end", () => {
    // Without an id the end frame can't find its start, so the card would render twice.
    expect(parseProgress({ session_id: "s1", phase: "tool_start", tool: "x" })).toBeNull();
  });

  it("ignores phases it doesn't render", () => {
    expect(parseProgress({ session_id: "s1", phase: "turn_started" })).toBeNull();
    expect(parseProgress({ session_id: "s1", phase: "reasoning", text: "hmm" })).toBeNull();
  });
});

describe("applyProgressFrame", () => {
  it("appends one live message on the first frame", () => {
    const out = applyProgressFrame([], text("Rolling…"));
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      id: liveMessageId("task-1", "s1"),
      role: "assistant",
      content: "Rolling…",
      status: "streaming",
      taskId: "task-1",
    });
  });

  it("grows the SAME message across frames — never a bubble per chunk", () => {
    let msgs: ChatMessage[] = [];
    msgs = applyProgressFrame(msgs, text("Three dice "));
    msgs = applyProgressFrame(msgs, text("and all Push Back."));
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe("Three dice and all Push Back.");
    expect(msgs[0].parts).toEqual([{ kind: "text", text: "Three dice and all Push Back." }]);
  });

  it("completes the SAME tool card on tool_end instead of adding a second", () => {
    let msgs: ChatMessage[] = [];
    msgs = applyProgressFrame(msgs, tool("tc1", "roll_block"));
    expect(msgs[0].toolCalls?.[0].status).toBe("running");
    msgs = applyProgressFrame(msgs, tool("tc1", "roll_block", true, "Push Back ×3"));
    expect(msgs[0].toolCalls).toHaveLength(1);
    expect(msgs[0].toolCalls?.[0]).toMatchObject({ status: "done", output: "Push Back ×3" });
    expect(msgs[0].parts).toEqual([{ kind: "tools", ids: ["tc1"] }]);
  });

  it("preserves frame-arrival order — narration reads above the tool it preceded", () => {
    let msgs: ChatMessage[] = [];
    msgs = applyProgressFrame(msgs, text("Blitzing the Gnoblar. "));
    msgs = applyProgressFrame(msgs, tool("tc1", "roll_block", true, "Push Back"));
    msgs = applyProgressFrame(msgs, text("Ball secured."));
    expect(msgs[0].parts).toEqual([
      { kind: "text", text: "Blitzing the Gnoblar. " },
      { kind: "tools", ids: ["tc1"] },
      { kind: "text", text: "Ball secured." },
    ]);
  });

  it("groups back-to-back tools into one block", () => {
    let msgs: ChatMessage[] = [];
    msgs = applyProgressFrame(msgs, tool("tc1", "a"));
    msgs = applyProgressFrame(msgs, tool("tc2", "b"));
    expect(msgs[0].parts).toEqual([{ kind: "tools", ids: ["tc1", "tc2"] }]);
  });

  it("marks a failed tool as error", () => {
    const msgs = applyProgressFrame([], tool("tc1", "boom", true, "nope", true));
    expect(msgs[0].toolCalls?.[0].status).toBe("error");
  });

  it("keeps existing messages ahead of the live one", () => {
    const prior: ChatMessage[] = [{ id: "u1", role: "user", content: "go" }];
    const msgs = applyProgressFrame(prior, text("working"));
    expect(msgs.map((m) => m.id)).toEqual(["u1", liveMessageId("task-1", "s1")]);
    expect(prior).toHaveLength(1); // input not mutated
  });

  it("keeps two concurrent server turns in separate bubbles", () => {
    // Two nudges can target one session (server-turn-store ref-counts for exactly this).
    let msgs: ChatMessage[] = [];
    msgs = applyProgressFrame(msgs, text("from A", "task-A"));
    msgs = applyProgressFrame(msgs, text("from B", "task-B"));
    expect(msgs).toHaveLength(2);
    expect(msgs.map((m) => m.content)).toEqual(["from A", "from B"]);
  });
});

describe("isLiveServerTurn", () => {
  it("identifies the bubble chat.resumed should replace", () => {
    const msgs = applyProgressFrame([], text("partial"));
    expect(isLiveServerTurn(msgs[0], "task-1", "s1")).toBe(true);
  });

  it("does not match an ordinary message", () => {
    expect(isLiveServerTurn({ id: "m1", role: "assistant", content: "x" }, "task-1", "s1")).toBe(false);
    expect(isLiveServerTurn({ role: "assistant", content: "x" }, "task-1", "s1")).toBe(false);
  });

  it("falls back to the session when the turn had no task id", () => {
    const msgs = applyProgressFrame([], { session: "s1", taskId: "", kind: "text", text: "x" });
    expect(isLiveServerTurn(msgs[0], "", "s1")).toBe(true);
  });
});
