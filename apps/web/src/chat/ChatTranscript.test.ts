import { act, createElement, Fragment, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";
import { CANCELLED_DELEGATION_PREFIX } from "./dismissedToolCalls";

const messageRender = vi.hoisted(() => vi.fn());

vi.mock("@protolabsai/ui/ai", () => ({
  Conversation: ({ children }: { children: unknown }) => children,
  Message: ({ children, queuedLabel, onCancel }: { children: ReactNode; queuedLabel?: string; onCancel?: () => void }) =>
    createElement(
      "div",
      null,
      queuedLabel ? createElement("span", null, queuedLabel) : null,
      children,
      onCancel ? createElement("button", { type: "button", "aria-label": "Cancel queued message", onClick: onCancel }) : null,
    ),
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
const steerQueue: { id: string; text: string; serverTaskId?: string }[] = [];

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

  it("labels queued server-turn interjections distinctly from normal steers — and gives them the same ✕", async () => {
    const onCancelSteer = vi.fn();
    await act(async () =>
      root.render(
        createElement(ChatTranscript, {
          sessionId: "server-session",
          messages: [],
          dismissedToolCalls,
          actions,
          steerQueue: [{ id: "i1", text: "Use the newest inbox item", serverTaskId: "task-9" }],
          serverTurnLabel: "running a scheduled task…",
          status: "idle",
          onCancelDelegation: noop,
          onDismissToolCall: noop,
          onCancelSteer,
        }),
      ),
    );

    expect(host.textContent).toContain("Use the newest inbox item");
    expect(host.textContent).toContain("queued interjection");
    // The live bug: a server-turn interjection rendered with no cancel affordance at all.
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Cancel queued message"]')!.click());
    expect(onCancelSteer).toHaveBeenCalledWith("i1");
  });

  it("never renders a message as queued once its id is settled in the transcript", async () => {
    await act(async () =>
      root.render(
        createElement(ChatTranscript, {
          sessionId: "server-session",
          messages: [{ id: "i1", role: "user", content: "yes 2024 as proposed", status: "done" }],
          dismissedToolCalls,
          actions,
          steerQueue: [
            { id: "i1", text: "yes 2024 as proposed", serverTaskId: "task-9" },
            { id: "i2", text: "and the resume too", serverTaskId: "task-9" },
          ],
          serverTurnLabel: "responding to background reports…",
          status: "idle",
          onCancelDelegation: noop,
          onDismissToolCall: noop,
          onCancelSteer: noop,
        }),
      ),
    );

    // i1 is a normal message now (the mocked row prints its content once); only i2 is pending.
    expect(host.textContent?.split("yes 2024 as proposed").length).toBe(2);
    expect(host.querySelectorAll('[aria-label="Cancel queued message"]')).toHaveLength(1);
    expect(host.textContent).toContain("and the resume too");
  });
});
