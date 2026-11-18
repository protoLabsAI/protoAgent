// A SETTLED server-initiated turn — a scheduled fire, watch reaction, inbox/webhook trigger,
// delegated task or self-improvement review — renders in the SAME raised card a scheduled
// result uses (.chat-scheduled-card), not a second, darker style. It is collapsed by default
// and opens in place to the full markdown answer plus the turn's tool calls. These mount the
// real ChatMessageView per origin, and pin which origins still stay full-size messages.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resumedTurnRender } from "../app/resumedTurn";
import type { ChatMessage } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";
import { preloadMarkdown } from "./LazyMarkdown";
import {
  GOAL_RUN_ORIGIN,
  SELF_IMPROVEMENT_ORIGIN,
  WAIT_RESUME_ORIGIN,
  effectiveOrigin,
  labelForOrigin,
  rendersAsResultCard,
  serverResultLabel,
} from "./server-turn-store";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ANSWER = "## Summary\nThe sweep found **three** stale assets.\n\n## Details\nEMEA is still pending review.";

let container: HTMLElement;
let root: Root;
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function settled(origin: string, overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: `m-${origin}`,
    role: "assistant",
    content: ANSWER,
    status: "done",
    origin,
    toolCalls: [{ id: "t1", name: "sweep_assets", input: "{}", output: "3 stale", status: "done" }],
    ...overrides,
  };
}

function render(message: ChatMessage) {
  act(() => root.render(h(QueryClientProvider, { client: qc }, h(ChatMessageView, { message }))));
}

const card = () => container.querySelector<HTMLElement>(".chat-scheduled-card");
const head = () => container.querySelector<HTMLElement>(".chat-scheduled-head")!;
const title = () => container.querySelector(".chat-scheduled-title")?.textContent ?? "";
const body = () => container.querySelector(".chat-scheduled-summary");

beforeEach(async () => {
  await preloadMarkdown();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as never;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("settled server-initiated turns wear the raised scheduled card", () => {
  const CASES: [origin: string, label: string][] = [
    ["scheduler", "Scheduled task"],
    ["watch-9f3", "Watch trigger"],
    ["inbox", "Inbox message"],
    ["webhook", "Webhook trigger"],
    ["a2a", "Delegated task"],
    ["background", "Background task"],
    [SELF_IMPROVEMENT_ORIGIN, "Self-improvement review"],
  ];

  it.each(CASES)("%s → raised card titled %s, collapsed, expanding to markdown + tool calls", (origin, label) => {
    render(settled(origin));

    // The raised card — and nothing of the retired dark one.
    expect(card()).not.toBeNull();
    expect(container.querySelector(".chat-scheduled")).not.toBeNull();
    expect(container.querySelector('[class*="chat-server-result"]')).toBeNull();
    expect(card()!.className).not.toContain("--failed");
    // Header row: the calendar/clock icon and the trigger's title, then a preview of the result.
    expect(head().querySelector("svg")).not.toBeNull();
    expect(title().startsWith(`${label} — `)).toBe(true);
    expect(title()).toContain("The sweep found three stale assets.");

    // Collapsed by default: no body, no tool card.
    expect(head().getAttribute("aria-expanded")).toBe("false");
    expect(body()).toBeNull();
    expect(container.textContent).not.toContain("sweep_assets");
    expect(container.textContent).not.toContain("EMEA is still pending review.");

    // Expands in place to the FULL markdown (rendered, not raw) and the turn's tool calls.
    act(() => head().click());
    expect(head().getAttribute("aria-expanded")).toBe("true");
    expect(card()!.className).toContain("chat-scheduled-card--open");
    expect(body()!.textContent).toContain("EMEA is still pending review.");
    expect(body()!.querySelector('[data-streamdown="strong"]')?.textContent).toBe("three");
    expect(body()!.textContent).toContain("sweep_assets");

    // …and collapses again.
    act(() => head().click());
    expect(body()).toBeNull();
  });

  it("toggles from the keyboard (Enter / Space), like the recurring chip", () => {
    render(settled("scheduler"));
    const press = (key: string) =>
      act(() => {
        head().dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
      });
    expect(head().getAttribute("role")).toBe("button");
    expect(head().tabIndex).toBe(0);
    press("Enter");
    expect(body()).not.toBeNull();
    press(" ");
    expect(body()).toBeNull();
    press("a");
    expect(body()).toBeNull();
  });

  it("marks a failed turn with the existing --failed modifier", () => {
    render(settled("scheduler", { status: "error", content: "**Turn failed:** boom" }));
    expect(card()!.className).toContain("chat-scheduled-card--failed");
    expect(title()).toBe("Scheduled task — Turn failed: boom");
  });

  it("says so when the turn produced no text", () => {
    render(settled("watch", { content: "", toolCalls: undefined }));
    expect(title()).toBe("Watch trigger — (no output)");
  });

  it("keeps expand state per message", () => {
    act(() =>
      root.render(
        h(
          QueryClientProvider,
          { client: qc },
          h(ChatMessageView, { message: settled("scheduler") }),
          h(ChatMessageView, { message: settled("inbox") }),
        ),
      ),
    );
    const heads = [...container.querySelectorAll<HTMLElement>(".chat-scheduled-head")];
    act(() => heads[0].click());
    expect(heads.map((el) => el.getAttribute("aria-expanded"))).toEqual(["true", "false"]);
  });

  it("leaves a still-streaming server turn full-size", () => {
    render(settled("scheduler", { status: "streaming" }));
    expect(card()).toBeNull();
  });
});

describe("the self-improvement review", () => {
  it("maps a self-improvement- trigger to its own origin, leaving other fires alone", () => {
    expect(effectiveOrigin("scheduler", "self-improvement-chat-1-1790542011776000")).toBe(SELF_IMPROVEMENT_ORIGIN);
    expect(effectiveOrigin("scheduler", "self-improvement")).toBe("scheduler");
    expect(effectiveOrigin("scheduler", "nightly-self-improvement-digest")).toBe("scheduler");
    expect(effectiveOrigin("scheduler", "job-1")).toBe("scheduler");
  });

  it("is labelled as a review and still renders as a card", () => {
    expect(serverResultLabel(SELF_IMPROVEMENT_ORIGIN)).toBe("Self-improvement review");
    expect(labelForOrigin(SELF_IMPROVEMENT_ORIGIN)).toMatch(/self-improvement review/i);
    expect(rendersAsResultCard(SELF_IMPROVEMENT_ORIGIN)).toBe(true);
  });

  it("carries the origin from the settled chat.resumed payload onto the card", () => {
    const r = resumedTurnRender({
      session_id: "chat-1",
      text: ANSWER,
      origin: "scheduler",
      trigger: "self-improvement-chat-1-1790542011776000",
    })!;
    expect(r.origin).toBe(SELF_IMPROVEMENT_ORIGIN);
    render(settled(r.origin, { content: r.content, status: r.status }));
    expect(title().startsWith("Self-improvement review — ")).toBe(true);
  });
});

describe("the conversation continuing is not carded", () => {
  it.each([GOAL_RUN_ORIGIN, WAIT_RESUME_ORIGIN, "background-resume", "delegate-result"])(
    "%s stays a full-size message with its answer and tool calls showing",
    (origin) => {
      render(settled(origin));
      expect(card()).toBeNull();
      expect(container.querySelector(".chat-scheduled")).toBeNull();
      expect(container.textContent).toContain("EMEA is still pending review.");
      expect(container.textContent).toContain("sweep_assets");
    },
  );

  it("nor is an operator-initiated answer (no origin)", () => {
    render(settled("", { origin: undefined }));
    expect(card()).toBeNull();
  });
});
