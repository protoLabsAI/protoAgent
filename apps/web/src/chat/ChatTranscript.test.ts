import { act, createElement, Fragment, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";

const messageRender = vi.hoisted(() => vi.fn());

vi.mock("@protolabsai/ui/ai", () => ({
  Conversation: ({ children }: { children: unknown }) => children,
  Message: ({ children }: { children: unknown }) => children,
}));

vi.mock("./ChatMessageView", () => ({
  ChatMessageView: ({ message }: { message: ChatMessage }) => {
    messageRender(message.id);
    return createElement("div", null, message.content);
  },
}));

import { ChatTranscript } from "./ChatTranscript";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const messages: ChatMessage[] = Array.from({ length: 150 }, (_, index) => ({
  id: `message-${index}`,
  role: index % 2 ? "assistant" : "user",
  content: `Large transcript row ${index}`,
  createdAt: index,
  status: "done",
}));

const noop = () => {};
const actions = {};
const dismissedToolCalls = new Set<string>();
const steerQueue: { id: string; text: string }[] = [];

function Harness() {
  const [draft, setDraft] = useState("");
  return createElement(
    Fragment,
    null,
    createElement("button", { type: "button", onClick: () => setDraft((value) => `${value}a`) }, "type"),
    createElement("span", { "data-testid": "draft" }, draft),
    createElement(ChatTranscript, {
      sessionId: "long-session",
      messages,
      dismissedToolCalls,
      actions,
      steerQueue,
      serverTurnLabel: null,
      status: "idle",
      onCancelDelegation: noop,
      onDismissToolCall: noop,
      onCancelSteer: noop,
    }),
  );
}

describe("ChatTranscript render isolation", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    messageRender.mockClear();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
  });

  it("does not render settled rows again when a long chat's draft changes", async () => {
    await act(async () => root.render(createElement(Harness)));
    expect(messageRender).toHaveBeenCalledTimes(150);

    const button = host.querySelector<HTMLButtonElement>("button")!;
    await act(async () => {
      button.click();
    });

    expect(host.querySelector('[data-testid="draft"]')?.textContent).toBe("a");
    expect(messageRender).toHaveBeenCalledTimes(150);
  });
});
