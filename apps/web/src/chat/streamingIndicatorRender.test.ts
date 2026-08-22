// Render-level proof for #2967: mid-turn streaming activity indicator. Once a streaming
// turn has ANY content (parts / toolCalls / flat content), the empty-message spinner never
// renders again — so generation pauses (between tool calls, during a long tool run, while
// large tool-call args stream) used to read as a dead stall. ChatMessageView now pins a
// small `.chat-streaming-indicator` spinner to the BOTTOM of the streaming message; DOM
// order pushes it below each new part as it arrives, and it unmounts when the turn settles.
// The fully-empty streaming turn must KEEP its original bare size-15 spinner (no wrapper).
// (Same jsdom mount pattern as backgroundChipRender.test.ts / waitRender.test.ts.)
import { afterEach, describe, expect, it } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ChatMessageView } from "./ChatMessageView";
import type { ChatMessage, ToolCall } from "../lib/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

/** Re-render the SAME root with an updated message (a new streamed frame arriving). */
async function rerender(message: ChatMessage): Promise<HTMLElement> {
  await act(async () => {
    root!.render(createElement(ChatMessageView, { message }));
  });
  return host!;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function msg(over: Partial<ChatMessage>): ChatMessage {
  return { id: "m1", role: "assistant", content: "", ...over };
}

const runningSearch: ToolCall = {
  id: "t1",
  name: "web_search",
  status: "running",
  input: JSON.stringify({ query: "coding agents" }),
};

const indicator = (el: HTMLElement) => el.querySelector<HTMLElement>(".chat-streaming-indicator");
const content = (el: HTMLElement) => el.querySelector<HTMLElement>(".pl-message__content")!;

describe("mid-turn streaming activity indicator (#2967)", () => {
  it("streaming turn with answer text: a small spinner renders at the BOTTOM of the message", async () => {
    const el = await render(
      msg({
        status: "streaming",
        content: "Hello there",
        parts: [{ kind: "text", text: "Hello there" }],
      }),
    );
    const ind = indicator(el);
    expect(ind).toBeTruthy();
    expect(el.querySelectorAll(".chat-streaming-indicator").length).toBe(1);
    // The small (size-12) spinner, not the size-15 empty-message one.
    const spinner = ind!.querySelector<HTMLElement>(".pl-spinner");
    expect(spinner).toBeTruthy();
    expect(spinner!.style.width).toBe("12px");
    // Bottom of the message: the indicator is the LAST element, below the answer text.
    expect(content(el).textContent).toContain("Hello there");
    expect(content(el).lastElementChild).toBe(ind);
  });

  it("streaming turn with only a running tool call (no text yet): indicator renders", async () => {
    const el = await render(
      msg({
        status: "streaming",
        parts: [{ kind: "tools", ids: ["t1"] }],
        toolCalls: [runningSearch],
      }),
    );
    expect(indicator(el)).toBeTruthy();
    expect(content(el).lastElementChild).toBe(indicator(el));
  });

  it("history-shaped streaming fallback (flat content, no parts): indicator renders", async () => {
    const el = await render(msg({ status: "streaming", content: "partial answer" }));
    expect(indicator(el)).toBeTruthy();
  });

  it("turn settled (status done): no indicator, no spinner at all", async () => {
    const el = await render(
      msg({
        status: "done",
        content: "Hello there",
        parts: [{ kind: "text", text: "Hello there" }],
      }),
    );
    expect(indicator(el)).toBeNull();
    expect(el.querySelector(".pl-spinner")).toBeNull();
  });

  it("fully-empty streaming turn keeps the BARE size-15 empty-message spinner (no regression)", async () => {
    const el = await render(msg({ status: "streaming" }));
    expect(indicator(el)).toBeNull();
    const spinner = content(el).querySelector<HTMLElement>(".pl-spinner");
    expect(spinner).toBeTruthy();
    expect(spinner!.style.width).toBe("15px");
    // Unwrapped — a direct child of the message content, exactly as before #2967.
    expect(spinner!.parentElement).toBe(content(el));
  });

  it("reasoning-only streaming turn: neither spinner — the live ReasoningCard is its own cue", async () => {
    const el = await render(msg({ status: "streaming", reasoning: "thinking about it…" }));
    expect(indicator(el)).toBeNull();
    expect(el.querySelector(".pl-spinner")).toBeNull();
    expect(el.querySelector(".reasoning-card")).toBeTruthy();
  });

  it("new content arriving mid-stream pushes the indicator below it via DOM order", async () => {
    const first = msg({
      status: "streaming",
      content: "Let me check.",
      parts: [{ kind: "text", text: "Let me check." }],
    });
    let el = await render(first);
    expect(content(el).lastElementChild).toBe(indicator(el));
    // Next frame: a tool call lands after the text — the indicator stays LAST, below it.
    el = await rerender({
      ...first,
      parts: [...first.parts!, { kind: "tools", ids: ["t1"] }],
      toolCalls: [runningSearch],
    });
    const ind = indicator(el)!;
    expect(content(el).lastElementChild).toBe(ind);
    const tool = content(el).querySelector(".tool-spotlight, .pl-toolcard, .pl-toolcard-summary")!;
    expect(tool).toBeTruthy();
    expect(ind.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
    // And when the turn then settles, the indicator disappears.
    el = await rerender({
      ...first,
      status: "done",
      toolCalls: [{ ...runningSearch, status: "done", output: "ok" }],
    });
    expect(indicator(el)).toBeNull();
  });
});
