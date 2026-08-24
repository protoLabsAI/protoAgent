// Render-level proof for #3095: dismissing a settled CANCELLED delegation card.
// Flow under test: (1) a running `task` delegation is cancelled — the tool_end frame
// (error: true + the graph/agent.py cancellation sentinel) settles the card and it renders
// with a dismiss ×; (2) clicking × reports the id; (3) the render-time filter removes the
// card while the REST of the transcript (other tool cards, the answer text) is unaffected.
// Guard rails: a completed call and a genuinely failed call never grow the × — their
// header actions are byte-for-byte what they were before this feature.
// (Same jsdom mount pattern as streamingIndicatorRender.test.ts / waitRender.test.ts.)
import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ChatMessageView } from "./ChatMessageView";
import { CANCELLED_DELEGATION_PREFIX, hideDismissedToolCalls, isCancelledDelegation } from "./dismissedToolCalls";
import { applyToolEvent } from "./turnReducers";
import type { ChatMessage, ToolCall } from "../lib/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(message: ChatMessage, onDismissToolCall?: (id: string) => void): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(ChatMessageView, { message, onDismissToolCall }));
  });
  return host;
}

async function rerender(message: ChatMessage, onDismissToolCall?: (id: string) => void): Promise<HTMLElement> {
  await act(async () => {
    root!.render(createElement(ChatMessageView, { message, onDismissToolCall }));
  });
  return host!;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

// The wire result graph/agent.py's `task` tool hands back on a Tier 2 cancel.
const cancelledOutput = `${CANCELLED_DELEGATION_PREFIX} before it finished: researcher — dig into the logs. Continue without its result.]`;

const cancelledTask: ToolCall = {
  id: "t-task",
  name: "task",
  input: JSON.stringify({ subagent_type: "researcher", description: "dig into the logs" }),
  output: cancelledOutput,
  status: "error",
};

const completedSearch: ToolCall = {
  id: "t-search",
  name: "web_search",
  input: JSON.stringify({ query: "coding agents" }),
  output: "10 results…",
  status: "done",
};

/** A settled turn holding the cancelled delegation + a completed call + the answer text —
 *  ordered parts, the shape the live stream leaves behind after the turn ends. */
function settledTurn(): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    status: "done",
    content: "Continuing without the delegation.",
    parts: [
      { kind: "tools", ids: ["t-task"] },
      { kind: "tools", ids: ["t-search"] },
      { kind: "text", text: "Continuing without the delegation." },
    ],
    toolCalls: [cancelledTask, completedSearch],
  };
}

const dismissBtn = (el: HTMLElement) => el.querySelector<HTMLButtonElement>(".tool-dismiss-btn");

describe("dismissable cancelled delegation card (#3095)", () => {
  it("a cancelled tool_end frame settles the card into the shape the dismiss keys off", () => {
    // Simulate the stream: start frame for the delegation, then the cancel's end frame
    // (error: true + sentinel output) — exactly what server/chat.py forwards.
    let msg: ChatMessage = { id: "m1", role: "assistant", content: "", status: "streaming", parts: [] };
    msg = applyToolEvent(msg, { id: "t-task", name: "task", phase: "start", input: cancelledTask.input });
    msg = applyToolEvent(msg, { id: "t-task", name: "task", phase: "end", output: cancelledOutput, error: true });
    const call = msg.toolCalls!.find((c) => c.id === "t-task")!;
    expect(call.status).toBe("error");
    expect(isCancelledDelegation(call)).toBe(true);
  });

  it("the cancelled card renders a dismiss ×; clicking it reports the tool-call id", async () => {
    const dismissed: string[] = [];
    const el = await render(settledTurn(), (id) => dismissed.push(id));
    // Both cards are on screen; only the cancelled delegation grew the ×.
    expect(el.querySelectorAll(".pl-toolcard").length).toBe(2);
    const btns = el.querySelectorAll(".tool-dismiss-btn");
    expect(btns.length).toBe(1);
    await act(async () => (btns[0] as HTMLButtonElement).click());
    expect(dismissed).toEqual(["t-task"]);
  });

  it("dismissing removes ONLY the cancelled card — the other card and the answer stay", async () => {
    const el = await render(settledTurn(), () => {});
    expect(el.querySelectorAll(".pl-toolcard").length).toBe(2);
    // The filter ChatSurface applies at render time, driven by the ×'d id.
    const filtered = hideDismissedToolCalls(settledTurn(), new Set(["t-task"]));
    const after = await rerender(filtered, () => {});
    expect(after.querySelectorAll(".pl-toolcard").length).toBe(1);
    expect(after.querySelector(".tool-dismiss-btn")).toBeNull();
    expect(after.textContent).toContain("web_search");
    expect(after.textContent).toContain("Continuing without the delegation.");
    expect(after.textContent).not.toContain("task");
  });

  it("stays dismissed on the reload-shaped (history, no parts) render too", async () => {
    const history: ChatMessage = {
      id: "m1",
      role: "assistant",
      status: "done",
      content: "Continuing without the delegation.",
      toolCalls: [cancelledTask],
    };
    const el = await render(hideDismissedToolCalls(history, new Set(["t-task"])), () => {});
    expect(el.querySelector(".pl-toolcard")).toBeNull();
    expect(el.textContent).toContain("Continuing without the delegation.");
  });

  it("a completed call renders NO dismiss affordance (unchanged chrome)", async () => {
    const el = await render(
      { id: "m1", role: "assistant", status: "done", content: "done", toolCalls: [completedSearch] },
      () => {},
    );
    expect(el.querySelector(".pl-toolcard")).toBeTruthy();
    expect(dismissBtn(el)).toBeNull();
  });

  it("a genuinely failed call renders NO dismiss affordance (unchanged chrome)", async () => {
    const failed: ToolCall = { ...completedSearch, id: "t-fail", output: "Error: connection refused", status: "error" };
    const el = await render(
      { id: "m1", role: "assistant", status: "done", content: "hm", toolCalls: [failed] },
      () => {},
    );
    expect(el.querySelector(".pl-toolcard")).toBeTruthy();
    expect(dismissBtn(el)).toBeNull();
  });

  it("a running delegation renders NO dismiss (Stop is its affordance, not ×)", async () => {
    const running: ToolCall = { ...cancelledTask, status: "running", output: undefined };
    const el = await render(
      { id: "m1", role: "assistant", status: "streaming", content: "", toolCalls: [running] },
      () => {},
    );
    expect(dismissBtn(el)).toBeNull();
  });

  it("without an onDismissToolCall handler the cancelled card renders no × (palette chat)", async () => {
    const el = await render({ id: "m1", role: "assistant", status: "done", content: "x", toolCalls: [cancelledTask] });
    expect(el.querySelector(".pl-toolcard")).toBeTruthy();
    expect(dismissBtn(el)).toBeNull();
  });
});
