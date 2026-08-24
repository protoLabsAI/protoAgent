// Local-only dismissal of CANCELLED delegation cards (#3095) — the pure logic.
// A stopped `task` delegation settles through the normal tool_end stream (error: true +
// the graph/agent.py cancellation sentinel as output), so it is permanent backend history;
// the dismiss must (a) fire ONLY on that exact shape — a completed call with real output
// and a real failure must render exactly as before — and (b) never mutate the message
// store, only filter the rendered copy.
import { beforeEach, describe, expect, it } from "vitest";

import type { ChatMessage, ToolCall } from "../lib/types";
import {
  CANCELLED_DELEGATION_PREFIX,
  dismissedToolCallSet,
  hideDismissedToolCalls,
  isCancelledDelegation,
  rememberDismissedToolCall,
} from "./dismissedToolCalls";

// The result graph/agent.py's `task` tool returns on a Tier 2 cancel, verbatim shape.
const cancelledOutput = `${CANCELLED_DELEGATION_PREFIX} before it finished: researcher — dig into the logs. Continue without its result.]`;

const cancelledTask: ToolCall = {
  id: "t-cancelled",
  name: "task",
  input: JSON.stringify({ subagent_type: "researcher", description: "dig into the logs" }),
  output: cancelledOutput,
  status: "error",
};

const completedSearch: ToolCall = {
  id: "t-done",
  name: "web_search",
  input: JSON.stringify({ query: "coding agents" }),
  output: "10 results…",
  status: "done",
};

const failedFetch: ToolCall = {
  id: "t-failed",
  name: "fetch_url",
  input: JSON.stringify({ url: "https://example.test" }),
  output: "Error: connection refused",
  status: "error",
};

beforeEach(() => {
  window.localStorage.clear();
});

describe("isCancelledDelegation", () => {
  it("matches the settled cancellation-sentinel task card", () => {
    expect(isCancelledDelegation(cancelledTask)).toBe(true);
  });

  it("matches whichever settled status the frame produced (done or error)", () => {
    // The frame today carries error: true, but the detection keys on the sentinel, not
    // the status flag — a server that settles the card green must not lose the dismiss.
    expect(isCancelledDelegation({ ...cancelledTask, status: "done" })).toBe(true);
  });

  it("never matches a running delegation (Stop, not dismiss, is its affordance)", () => {
    expect(isCancelledDelegation({ ...cancelledTask, status: "running", output: undefined })).toBe(false);
  });

  it("never matches a completed call with a real result", () => {
    expect(isCancelledDelegation(completedSearch)).toBe(false);
    expect(isCancelledDelegation({ ...cancelledTask, output: "Found it: the answer is 42." })).toBe(false);
  });

  it("never matches a real failure", () => {
    expect(isCancelledDelegation(failedFetch)).toBe(false);
    expect(isCancelledDelegation({ ...cancelledTask, output: "[researcher subagent crashed. Continue without its result.]" })).toBe(false);
  });

  it("never matches a non-task card, even with a sentinel-shaped output", () => {
    expect(isCancelledDelegation({ ...cancelledTask, name: "web_search" })).toBe(false);
  });
});

describe("dismissed-set persistence (localStorage)", () => {
  it("round-trips: a remembered id is in a fresh read (survives a 'reload')", () => {
    rememberDismissedToolCall("t-cancelled");
    expect(dismissedToolCallSet().has("t-cancelled")).toBe(true);
  });

  it("returns a NEW Set each time — safe as React state (identity change re-renders)", () => {
    const a = rememberDismissedToolCall("x");
    const b = rememberDismissedToolCall("y");
    expect(a).not.toBe(b);
    expect(b.has("x")).toBe(true);
    expect(b.has("y")).toBe(true);
  });

  it("caps the stored set at 300, evicting oldest first", () => {
    for (let i = 0; i < 305; i++) rememberDismissedToolCall(`id-${i}`);
    const stored = dismissedToolCallSet();
    expect(stored.size).toBe(300);
    expect(stored.has("id-0")).toBe(false);
    expect(stored.has("id-304")).toBe(true);
  });

  it("shrugs off corrupted storage instead of throwing", () => {
    window.localStorage.setItem("protoagent.chat.dismissedToolCalls", "not json");
    expect(dismissedToolCallSet().size).toBe(0);
    expect(rememberDismissedToolCall("t1").has("t1")).toBe(true);
  });
});

describe("hideDismissedToolCalls", () => {
  const msg = (toolCalls?: ToolCall[]): ChatMessage => ({
    id: "m1",
    role: "assistant",
    content: "Continuing without the delegation's result.",
    status: "done",
    toolCalls,
  });

  it("strips the dismissed cancelled card AND its nested subagent tools", () => {
    const child: ToolCall = { ...completedSearch, id: "t-kid", parentId: "t-cancelled" };
    const out = hideDismissedToolCalls(msg([cancelledTask, child, completedSearch]), new Set(["t-cancelled"]));
    expect(out.toolCalls?.map((c) => c.id)).toEqual(["t-done"]);
    // Everything else on the message is untouched.
    expect(out.content).toBe("Continuing without the delegation's result.");
  });

  it("returns the SAME message object when nothing applies (no phantom re-renders)", () => {
    const m = msg([completedSearch]);
    expect(hideDismissedToolCalls(m, new Set())).toBe(m);
    expect(hideDismissedToolCalls(m, new Set(["t-elsewhere"]))).toBe(m);
    const noTools = msg(undefined);
    expect(hideDismissedToolCalls(noTools, new Set(["t-cancelled"]))).toBe(noTools);
  });

  it("never hides a completed or genuinely failed call, even if its id is in the set", () => {
    // The guard re-checks the sentinel per call — a stale/colliding id in localStorage
    // can never make a real result disappear.
    const m = msg([completedSearch, failedFetch]);
    expect(hideDismissedToolCalls(m, new Set(["t-done", "t-failed"]))).toBe(m);
  });

  it("does not mutate the original message (backend-mirroring store stays intact)", () => {
    const m = msg([cancelledTask, completedSearch]);
    hideDismissedToolCalls(m, new Set(["t-cancelled"]));
    expect(m.toolCalls?.map((c) => c.id)).toEqual(["t-cancelled", "t-done"]);
  });
});
