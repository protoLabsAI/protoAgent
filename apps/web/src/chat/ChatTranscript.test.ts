import { act, createElement, Fragment, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

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

// The DS Tooltip is Radix-backed (portal + hover/focus + open-delay timers): driving it OPEN
// in jsdom is exactly what hangs a render test. Stub it to render its `label` eagerly so the
// full sent-time content behind the sent-timestamp widget is assertable without a portal/hover
// cycle. Only the real ChatMessageView (loaded via vi.importActual below) consumes this; the
// transcript-isolation suites mock ./ChatMessageView and never touch overlays.
vi.mock("@protolabsai/ui/overlays", () => ({
  Tooltip: ({ label, children }: { label: ReactNode; children: ReactNode }) =>
    createElement(
      "span",
      { "data-tip": true },
      children,
      createElement("span", { "data-testid": "tip-label" }, label),
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

// The sent-timestamp footer widget (#3448) lives in the SHARED ChatMessageView renderer, so
// proving it on that renderer proves it for both consumers (main chat + palette chat). The real
// renderer is pulled in with vi.importActual because this file mocks ./ChatMessageView for the
// transcript-isolation suites above.
describe("ChatMessageView sent-timestamp footer (#3448)", () => {
  let ChatMessageView: (props: { message: ChatMessage }) => ReactNode;
  let tsHost: HTMLDivElement | null = null;
  let tsRoot: Root | null = null;

  beforeAll(async () => {
    ({ ChatMessageView } = (await vi.importActual("./ChatMessageView")) as {
      ChatMessageView: (props: { message: ChatMessage }) => ReactNode;
    });
  });

  async function render(message: ChatMessage): Promise<HTMLElement> {
    // Tear down any prior mount so a multi-render test (the invalid-input sweep) doesn't leak
    // roots or leave a stale host in the query scope.
    if (tsRoot) await act(async () => tsRoot!.unmount());
    tsHost?.remove();
    tsHost = document.createElement("div");
    document.body.appendChild(tsHost);
    await act(async () => {
      tsRoot = createRoot(tsHost!);
      tsRoot.render(createElement(ChatMessageView, { message }));
    });
    return tsHost;
  }

  /** Re-render the SAME root — a streamed turn settling in place. */
  async function rerender(message: ChatMessage): Promise<HTMLElement> {
    await act(async () => tsRoot!.render(createElement(ChatMessageView, { message })));
    return tsHost!;
  }

  afterEach(async () => {
    await act(async () => tsRoot?.unmount());
    tsHost?.remove();
    tsRoot = null;
    tsHost = null;
  });

  // A FIXED epoch (never the wall clock), formatted with the SAME runtime Intl the widget uses,
  // so the assertions hold under any CI locale/timezone rather than pinning a literal string.
  const SENT = 1_700_000_000_000;
  const sent = new Date(SENT);
  const shortLabel = sent.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const fullLabel = sent.toLocaleString([], { dateStyle: "full", timeStyle: "medium" });

  const chip = (el: HTMLElement) => el.querySelector<HTMLElement>(".chat-sent-time");

  it("renders the widget for a settled assistant message with a valid createdAt", async () => {
    const el = await render({ id: "a1", role: "assistant", content: "Done.", status: "done", createdAt: SENT });
    expect(chip(el)).toBeTruthy();
    expect(el.querySelector(".chat-sent-time-label")?.textContent).toBe(shortLabel);
    expect(chip(el)!.querySelector("svg")).toBeTruthy(); // the reused Clock icon
  });

  it("renders the widget for a settled user message too (shared renderer → both consumers)", async () => {
    const el = await render({ id: "u1", role: "user", content: "Hi", status: "done", createdAt: SENT });
    expect(chip(el)).toBeTruthy();
    expect(el.querySelector(".chat-sent-time-label")?.textContent).toBe(shortLabel);
  });

  it("exposes the full local date-and-time via the DS Tooltip plus a focusable accessible name", async () => {
    const el = await render({ id: "a2", role: "assistant", content: "Done.", status: "done", createdAt: SENT });
    // The full timestamp rides the DS Tooltip's label (no bespoke tooltip).
    expect(el.querySelector('[data-testid="tip-label"]')?.textContent).toBe(fullLabel);
    // Keyboard/AT path: the chip is focusable and names the full sent time.
    expect(chip(el)!.getAttribute("tabindex")).toBe("0");
    expect(chip(el)!.getAttribute("aria-label")).toBe(`Sent ${fullLabel}`);
  });

  it("renders NO widget for missing, zero, negative, non-finite, or invalid timestamps", async () => {
    for (const createdAt of [undefined, 0, -1, Number.NaN, Number.POSITIVE_INFINITY, 8.64e15 + 1]) {
      const el = await render({ id: "x", role: "assistant", content: "hi", status: "done", createdAt });
      expect(chip(el)).toBeNull();
      expect(el.textContent).not.toContain("Invalid Date");
    }
  });

  it("hides the widget while streaming, then shows it once the SAME message settles", async () => {
    const live: ChatMessage = { id: "live", role: "assistant", content: "partial", status: "streaming", createdAt: SENT };
    let el = await render(live);
    expect(chip(el)).toBeNull();
    el = await rerender({ ...live, status: "done", content: "final" });
    expect(chip(el)).toBeTruthy();
    expect(el.querySelector(".chat-sent-time-label")?.textContent).toBe(shortLabel);
  });

  it("leaves a specialized card (background report) untouched — no sent-time footer on it", async () => {
    const el = await render({
      id: "r1",
      role: "system",
      content: "Report ready",
      status: "done",
      createdAt: SENT,
      report: { jobId: "job-1", title: "Nightly digest" },
    });
    expect(el.querySelector(".chat-report-chip")).toBeTruthy();
    expect(chip(el)).toBeNull();
  });
});
