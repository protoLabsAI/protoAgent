// #3946: a turn PARKED on the operator (input-required) in a fresh browser kept its
// `ask_human` card's spinner and climbing timer, and an empty bubble showed the streaming
// placeholder, for as long as the form went unanswered. After #3935 the SESSION settles on
// input-required; this pins that the BUBBLE and its in-flight card render as paused too —
// at hydration, on a reattach's paused settle, and cleared again when the answer lands.
// (Same jsdom mount pattern as dismissedToolCallsRender.test.ts.)
import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import type { DurableChatTurn } from "../lib/api";
import type { ChatMessage } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";
import { markTurnPaused, settleAnsweredPause } from "./reattach";
import { messagesFromDurableTurn } from "./sessionHydration";
import { applyToolEvent, unpauseBubble } from "./turnReducers";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TOOL = "https://proto-labs.ai/a2a/ext/tool-call-v1";

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(message: ChatMessage): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(ChatMessageView, { message }));
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

/** An `ask_human` call in flight, started a minute ago — past the elapsed-chip threshold. */
function parkedBubble(extra: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "a1",
    role: "assistant",
    content: "",
    status: "streaming",
    taskId: "t1",
    toolCalls: [{ id: "ask-1", name: "ask_human", status: "running", startedAt: Date.now() - 60_000 }],
    parts: [{ kind: "tools", ids: ["ask-1"] }],
    ...extra,
  };
}

describe("a paused turn renders as waiting, not streaming (#3946)", () => {
  it("control: an unpaused in-flight card spins with a timer and the bubble streams", async () => {
    const el = await render(parkedBubble());
    expect(el.querySelector(".pl-toolcard__status--running")).not.toBeNull();
    expect(el.querySelector(".tool-elapsed")).not.toBeNull();
    expect(el.querySelector(".chat-streaming-indicator")).not.toBeNull();
    expect(el.querySelector(".chat-paused-indicator")).toBeNull();
  });

  it("a paused bubble: no spinner, no timer, no streaming indicator — a waiting cue instead", async () => {
    const [paused] = markTurnPaused([parkedBubble()], "a1");
    const el = await render(paused);
    expect(el.querySelector(".pl-toolcard__status--running")).toBeNull();
    expect(el.querySelector(".tool-elapsed")).toBeNull();
    expect(el.querySelector(".tool-paused")).not.toBeNull();
    expect(el.querySelector(".tool-waiting")?.textContent).toContain("waiting for you");
    expect(el.querySelector(".chat-streaming-indicator")).toBeNull();
    expect(el.querySelector(".chat-paused-indicator")?.textContent).toContain("Waiting for your input");
  });

  it("control: an EMPTY streaming bubble shows the placeholder spinner", async () => {
    const el = await render(parkedBubble({ toolCalls: undefined, parts: undefined }));
    expect(el.querySelector(".pl-spinner")).not.toBeNull();
  });

  it("an EMPTY paused bubble shows the waiting cue, not the streaming placeholder spinner", async () => {
    const [paused] = markTurnPaused([parkedBubble({ toolCalls: undefined, parts: undefined })], "a1");
    const el = await render(paused);
    expect(el.querySelector(".pl-spinner, .chat-streaming-indicator")).toBeNull();
    expect(el.querySelector(".chat-paused-indicator")).not.toBeNull();
  });
});

describe("paused state lifecycle (#3946)", () => {
  it("markTurnPaused keeps the turn streaming (the server owns it) and marks running cards", () => {
    const done = { id: "c0", name: "web_search", status: "done" as const };
    const [m] = markTurnPaused([parkedBubble({ toolCalls: [done, ...parkedBubble().toolCalls!] })], "a1");
    expect(m.status).toBe("streaming");
    expect(m.paused).toBe(true);
    expect(m.toolCalls?.map((c) => c.paused)).toEqual([undefined, true]);
    // A settled bubble is never marked.
    const settled = { ...parkedBubble(), status: "done" as const };
    expect(markTurnPaused([settled], "a1")[0]).toBe(settled);
  });

  it("a form answer settles the paused bubble and clears the pause", () => {
    const [answered] = settleAnsweredPause(markTurnPaused([parkedBubble()], "a1"), "a1");
    expect(answered).toMatchObject({ status: "done", toolCalls: [{ name: "ask_human", status: "done" }] });
    expect(answered.paused).toBeUndefined();
    expect(answered.toolCalls?.[0].paused).toBeUndefined();
  });

  it("an approval resume continuing the bubble clears the pause; the card's end frame settles it", () => {
    const [paused] = markTurnPaused([parkedBubble()], "a1");
    const resumed = unpauseBubble(paused);
    expect(resumed.paused).toBeUndefined();
    expect(resumed.toolCalls?.[0].paused).toBeUndefined();
    const ended = applyToolEvent(paused, { id: "ask-1", name: "ask_human", phase: "end", output: "ok" });
    expect(ended.toolCalls?.[0]).toMatchObject({ status: "done" });
    expect(ended.toolCalls?.[0].paused).toBeUndefined();
  });

  it("hydration marks a durable turn parked on input-required as paused from the first paint", () => {
    const parked: DurableChatTurn = {
      task_id: "task-9",
      state: "TASK_STATE_INPUT_REQUIRED",
      last_updated: "2026-09-30T12:00:00Z",
      text: "",
      status: { state: "TASK_STATE_INPUT_REQUIRED" },
      artifacts: [],
      history: [
        { role: "ROLE_USER", parts: [{ text: "pick a fruit" }] },
        { role: "ROLE_AGENT", metadata: { [TOOL]: { toolCallId: "ask-1", name: "ask_human", phase: "started", args: "" } } },
      ],
    } as DurableChatTurn;
    const assistant = messagesFromDurableTurn(parked).find((m) => m.role === "assistant")!;
    expect(assistant.status).toBe("streaming"); // still reattachable: the form re-renders off it
    expect(assistant.paused).toBe(true);
    expect(assistant.toolCalls?.[0]).toMatchObject({ name: "ask_human", status: "running", paused: true });

    const working = messagesFromDurableTurn({ ...parked, state: "TASK_STATE_WORKING", status: { state: "TASK_STATE_WORKING" } });
    expect(working.find((m) => m.role === "assistant")!.paused).toBeUndefined();
  });
});
