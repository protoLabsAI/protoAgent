// The per-tool-call-id streamed-argument buffer (ADR 0118 D3, S3 console decode). The buffer
// reassembles `tool-args-v1` slices into each tool call's decoded argument text, tolerating
// out-of-order and duplicated/overlapping frames, and is LIVE-ONLY — cleared at turn end and
// never populated from a hydration/reattach replay. Nothing renders it yet (S8 consumes it).

import { describe, expect, it, vi } from "vitest";

import type { DurableChatTurn } from "../lib/api/a2aStream";
import { makeA2ADispatcher, replayDurableChatTurn, toolArgsFromParts } from "../lib/api/a2aStream";
import { appendToolArgs, emptyToolArgs, toToolArgsBuffer, type ToolArgsBuffers } from "./toolArgsBuffer";
import { createToolArgsTracker } from "./turnReducers";

const CTX = "chat-1";
const TOOL_ARGS_MIME = "application/vnd.protolabs.tool-args-v1+json";

/** Fold a run of frames (for one id) into a fresh map and return the public preview. */
function fold(id: string, frames: Array<{ offset: number; chunk: string; done?: boolean }>) {
  let buffers: ToolArgsBuffers = emptyToolArgs();
  for (const f of frames) {
    buffers = appendToolArgs(buffers, { id, arg: "code", offset: f.offset, chunk: f.chunk, done: f.done ?? false });
  }
  return toToolArgsBuffer(buffers, id);
}

describe("toolArgsFromParts — decode a tool-args-v1 DataPart", () => {
  const part = (data: Record<string, unknown>) => [{ data, metadata: { mimeType: TOOL_ARGS_MIME } }];

  it("reads {id, arg, offset, chunk, done} and flooring a proto-JSON float offset", () => {
    expect(toolArgsFromParts(part({ id: "c1", arg: "code", offset: 3.0, chunk: "lo", done: false }))).toEqual({
      id: "c1",
      arg: "code",
      offset: 3,
      chunk: "lo",
      done: false,
    });
  });

  it("tolerates a terminal frame with just done (no chunk), and a missing/negative offset", () => {
    expect(toolArgsFromParts(part({ id: "c1", arg: "code", done: true }))).toEqual({
      id: "c1",
      arg: "code",
      offset: 0,
      chunk: "",
      done: true,
    });
    expect(toolArgsFromParts(part({ id: "c1", offset: -5, chunk: "x" }))?.offset).toBe(0);
  });

  it("returns null without a binding tool-call id, and null for a non-matching part", () => {
    expect(toolArgsFromParts(part({ arg: "code", offset: 0, chunk: "x" }))).toBeNull();
    expect(toolArgsFromParts([{ data: { id: "c1" }, metadata: { mimeType: "application/json" } }])).toBeNull();
    expect(toolArgsFromParts(undefined)).toBeNull();
  });
});

describe("appendToolArgs — reassemble one tool call's argument (r1)", () => {
  it("in-order contiguous frames concatenate, and done latches on the terminal frame", () => {
    const buf = fold("c1", [
      { offset: 0, chunk: "hel" },
      { offset: 3, chunk: "lo" },
      { offset: 5, chunk: "", done: true },
    ]);
    expect(buf).toEqual({ arg: "code", text: "hello", done: true });
  });

  it("out-of-order frames still produce the correct text once the gap fills", () => {
    // The tail arrives first (nothing renderable yet), then the head completes it.
    const partial = fold("c1", [{ offset: 3, chunk: "lo" }]);
    expect(partial?.text).toBe("");
    const whole = fold("c1", [
      { offset: 3, chunk: "lo" },
      { offset: 0, chunk: "hel", done: true },
    ]);
    expect(whole).toEqual({ arg: "code", text: "hello", done: true });
  });

  it("a fully-duplicated frame is a no-op; an overlapping frame drops its overlapping prefix", () => {
    const dup = fold("c1", [
      { offset: 0, chunk: "hel" },
      { offset: 0, chunk: "hel" }, // exact resend — dropped
      { offset: 2, chunk: "llo" }, // overlaps "l" at offset 2 — only "lo" is new
    ]);
    expect(dup?.text).toBe("hello");
  });

  it("reorders a scrambled, duplicated burst into the one correct value", () => {
    const buf = fold("c1", [
      { offset: 3, chunk: "ld" }, // tail arrives first
      { offset: 0, chunk: "wor" }, // head closes the gap → "world"
      { offset: 3, chunk: "ld" }, // duplicate, already placed → no-op
      { offset: 5, chunk: "", done: true },
    ]);
    expect(buf).toEqual({ arg: "code", text: "world", done: true });
  });

  it("counts offsets in CODE POINTS, so a non-BMP char (emoji) never shifts the reassembly", () => {
    // The server measures `offset`/`chunk` length in Python code points; a JS `.length`
    // (UTF-16 code units) double-counts "😀", so frame 2 at code-point offset 1 would look
    // like it overlaps and drop its first char — rendering "😀bc" instead of "😀abc".
    const contiguous = fold("c1", [
      { offset: 0, chunk: "😀" }, // one code point (two UTF-16 units)
      { offset: 1, chunk: "abc", done: true },
    ]);
    expect(contiguous).toEqual({ arg: "code", text: "😀abc", done: true });

    // Out-of-order across an emoji: the tail lands first and waits, the head closes the gap.
    const reordered = fold("c1", [
      { offset: 2, chunk: "b" }, // after "a😀" — stashed ahead of the gap
      { offset: 0, chunk: "a😀", done: true },
    ]);
    expect(reordered).toEqual({ arg: "code", text: "a😀b", done: true });

    // A resend overlapping an emoji is still a pure no-op (nothing re-appended or dropped).
    const overlap = fold("c1", [
      { offset: 0, chunk: "a😀b" }, // code points: a(0) 😀(1) b(2)
      { offset: 1, chunk: "😀b" }, // wholly behind the end → duplicate
    ]);
    expect(overlap?.text).toBe("a😀b");
  });

  it("keeps separate tool calls independent, and leaves the input map untouched (pure)", () => {
    const first = appendToolArgs(emptyToolArgs(), { id: "a", arg: "code", offset: 0, chunk: "aa", done: false });
    const second = appendToolArgs(first, { id: "b", arg: "query", offset: 0, chunk: "bb", done: true });
    expect(toToolArgsBuffer(second, "a")).toEqual({ arg: "code", text: "aa", done: false });
    expect(toToolArgsBuffer(second, "b")).toEqual({ arg: "query", text: "bb", done: true });
    // `first` was not mutated by the second append.
    expect(Object.keys(first)).toEqual(["a"]);
    expect(toToolArgsBuffer(emptyToolArgs(), "a")).toBeUndefined();
  });
});

describe("createToolArgsTracker — live-turn wiring off the frame dispatcher", () => {
  const workingFrame = (data: Record<string, unknown>) => ({
    result: {
      statusUpdate: {
        taskId: "t1",
        contextId: CTX,
        status: { state: "TASK_STATE_WORKING", message: { parts: [{ data, metadata: { mimeType: TOOL_ARGS_MIME } }] } },
      },
    },
  });

  it("decodes live WORKING frames through the dispatcher and reassembles out-of-order (r1)", () => {
    const tracker = createToolArgsTracker();
    const dispatch = makeA2ADispatcher(CTX, { onToolArgs: (evt) => tracker.push(evt) });
    dispatch(workingFrame({ id: "c1", arg: "code", offset: 5, chunk: "('hi')", done: false }) as never);
    dispatch(workingFrame({ id: "c1", arg: "code", offset: 0, chunk: "print", done: false }) as never);
    dispatch(workingFrame({ id: "c1", arg: "code", offset: 11, chunk: "", done: true }) as never);
    expect(tracker.get("c1")).toEqual({ arg: "code", text: "print('hi')", done: true });
    expect(tracker.size).toBe(1);
  });

  it("clears every preview at turn end — the state never outlives its turn (r2)", () => {
    const tracker = createToolArgsTracker();
    tracker.push({ id: "c1", arg: "code", offset: 0, chunk: "ab", done: false });
    tracker.push({ id: "c2", arg: "q", offset: 0, chunk: "xy", done: true });
    expect(tracker.size).toBe(2);
    expect(tracker.all()).toEqual({ c1: { arg: "code", text: "ab", done: false }, c2: { arg: "q", text: "xy", done: true } });
    tracker.clear();
    expect(tracker.size).toBe(0);
    expect(tracker.get("c1")).toBeUndefined();
    expect(tracker.all()).toEqual({});
  });
});

describe("live-only — never rebuilt on hydration/reattach (r2)", () => {
  it("a durable-turn (hydration) replay never emits onToolArgs, even if history carries a tool-args part", () => {
    const durable: DurableChatTurn = {
      task_id: "t1",
      state: "TASK_STATE_COMPLETED",
      last_updated: null,
      text: "print('hi')",
      status: { state: "TASK_STATE_COMPLETED" },
      artifacts: [{ parts: [{ text: "print('hi')" }] }],
      history: [
        { role: "ROLE_USER", parts: [{ text: "write code" }] },
        {
          role: "ROLE_AGENT",
          // A stale partial that should never have been persisted — snapshot replay must ignore it.
          parts: [{ data: { id: "c1", arg: "code", offset: 0, chunk: "sta", done: false }, metadata: { mimeType: TOOL_ARGS_MIME } }],
        } as never,
      ],
    };
    const onToolArgs = vi.fn();
    const onText = vi.fn();
    replayDurableChatTurn(durable, CTX, { onToolArgs, onText });
    expect(onToolArgs).not.toHaveBeenCalled();
    // The finished artifact still replays — a consumer that ignores tool-args gets the answer.
    expect(onText).toHaveBeenCalledWith("print('hi')", false);
  });

  it("a Task snapshot frame (reattach) carrying a tool-args part in history does not populate the tracker", () => {
    const tracker = createToolArgsTracker();
    const dispatch = makeA2ADispatcher(CTX, { onToolArgs: (evt) => tracker.push(evt) });
    dispatch({
      result: {
        task: {
          id: "t1",
          contextId: CTX,
          status: { state: "TASK_STATE_WORKING" },
          artifacts: [{ parts: [{ text: "so far" }] }],
          history: [
            {
              role: "ROLE_AGENT",
              parts: [{ data: { id: "c1", arg: "code", offset: 0, chunk: "sta", done: false }, metadata: { mimeType: TOOL_ARGS_MIME } }],
            },
          ],
        },
      },
    } as never);
    expect(tracker.size).toBe(0);
  });
});
