import { act, createElement, Fragment, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";
import { CANCELLED_DELEGATION_PREFIX } from "./dismissedToolCalls";

const messageRender = vi.hoisted(() => vi.fn());

vi.mock("@protolabsai/ui/ai", () => ({
  Conversation: ({ children }: { children: unknown }) => children,
  Message: ({ children, queuedLabel }: { children: ReactNode; queuedLabel?: string }) =>
    createElement("div", null, queuedLabel ? createElement("span", null, queuedLabel) : null, children),
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
const serverInterjectionQueue: { id: string; text: string }[] = [];

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
      serverInterjectionQueue,
      serverTurnLabel: null,
      status: "idle",
      onCancelDelegation: noop,
      onDismissToolCall: noop,
      onCancelSteer: noop,
    }),
  );
}

const dismissedMessage: ChatMessage = {
  id: "settled-with-dismissal",
  role: "assistant",
  content: "Settled answer",
  status: "done",
  toolCalls: [
    {
      id: "cancelled-task",
      name: "task",
      input: "{}",
      output: `${CANCELLED_DELEGATION_PREFIX}]`,
      status: "error",
    },
  ],
};
const dismissedTask = new Set(["cancelled-task"]);

function StreamingHarness() {
  const [content, setContent] = useState("first frame");
  const streamingMessage: ChatMessage = {
    id: "live-row",
    role: "assistant",
    content,
    status: "streaming",
  };
  return createElement(
    Fragment,
    null,
    createElement("button", { type: "button", onClick: () => setContent("next frame") }, "stream"),
    createElement(ChatTranscript, {
      sessionId: "streaming-session",
      messages: [dismissedMessage, streamingMessage],
      dismissedToolCalls: dismissedTask,
      actions,
      steerQueue,
      serverInterjectionQueue,
      serverTurnLabel: null,
      status: "streaming",
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

  it("keeps a filtered settled row stable when another row streams", async () => {
    await act(async () => root.render(createElement(StreamingHarness)));
    expect(messageRender.mock.calls.map(([id]) => id)).toEqual(["settled-with-dismissal", "live-row"]);

    await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());

    expect(messageRender.mock.calls.map(([id]) => id)).toEqual([
      "settled-with-dismissal",
      "live-row",
      "live-row",
    ]);
  });

  it("labels queued server-turn interjections distinctly from normal steers", async () => {
    await act(async () =>
      root.render(
        createElement(ChatTranscript, {
          sessionId: "server-session",
          messages: [],
          dismissedToolCalls,
          actions,
          steerQueue,
          serverInterjectionQueue: [{ id: "i1", text: "Use the newest inbox item" }],
          serverTurnLabel: "running a scheduled task…",
          status: "idle",
          onCancelDelegation: noop,
          onDismissToolCall: noop,
          onCancelSteer: noop,
        }),
      ),
    );

    expect(host.textContent).toContain("Use the newest inbox item");
    expect(host.textContent).toContain("queued interjection");
  });
});
