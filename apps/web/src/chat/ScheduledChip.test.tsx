// A recurring scheduled fire renders as the compact one-line ScheduledChip (server sets
// `scheduled.collapse`) so an hourly job can't fill the chat with cards (#2990). It used to be
// a dead end — the result was only a 90-char hover tooltip away. #4726 makes the chip EXPAND in
// place into the same body the first-fire ScheduledReportCard shows: the full summary, the fire
// time, and a working "View full result". These mount the real ChatMessageView through that
// collapsed → expanded → collapsed sequence, exercising mouse, keyboard, and dismiss.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useDocViewer } from "../docviewer";
import type { ChatMessage } from "../lib/types";
import { ChatMessageView } from "./ChatMessageView";
import { preloadMarkdown } from "./LazyMarkdown";
import { plainPreviewText, serverResultPreview } from "./server-turn-store";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// A summary longer than the chip's 90-char one-line cap, so the collapsed line is truncated
// (ends in …) while the expanded body must show the WHOLE thing.
const SUMMARY =
  "The brand-plan sweep flagged 3 stale assets and refreshed the Q4 messaging deck across all " +
  "six regional variants, with the EMEA cut still pending legal review before it can ship.";

let container: HTMLElement;
let root: Root;
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function scheduledMessage(overrides: Partial<NonNullable<ChatMessage["scheduled"]>> = {}): ChatMessage {
  return {
    id: "sched-1",
    role: "system",
    content: "",
    status: "done",
    scheduled: {
      jobId: "brand-plan-sweep",
      firedAt: "2026-11-03T14:00:00.000Z",
      summary: SUMMARY,
      collapse: true,
      ...overrides,
    },
  };
}

function render(message: ChatMessage) {
  act(() => root.render(h(QueryClientProvider, { client: qc }, h(ChatMessageView, { message }))));
}

const toggle = () => container.querySelector<HTMLElement>(".chat-scheduled-chip-toggle")!;
const body = () => container.querySelector(".chat-scheduled-chip-body");
const dismissBtn = () => container.querySelector<HTMLButtonElement>('button[aria-label="Dismiss scheduled result"]')!;
const viewFullResult = () =>
  [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === "View full result");

function clickKey(el: HTMLElement, key: string) {
  act(() => {
    el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
  });
}

beforeEach(async () => {
  await preloadMarkdown();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as never;
  // Each test starts with a clean dismissed-set (localStorage keyed per fire) and a closed viewer.
  localStorage.clear();
  act(() => useDocViewer.setState({ open: false, doc: null }));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("ScheduledChip — a collapsed repeat fire expands into the full result (#4726)", () => {
  it("renders the compact one line collapsed, with the result body hidden", () => {
    render(scheduledMessage());
    const t = toggle();
    expect(t).toBeTruthy();
    // The one-liner names the job and is truncated (the 90-char cap adds an ellipsis).
    expect(t.textContent).toContain("Scheduled task brand-plan-sweep ran");
    expect(t.textContent).toContain("…");
    // Collapsed: aria-expanded is false and there is no body (no full summary, no action).
    expect(t.getAttribute("aria-expanded")).toBe("false");
    expect(body()).toBeNull();
    expect(viewFullResult()).toBeUndefined();
    // The dismiss control is always present…
    expect(dismissBtn()).toBeTruthy();
    // …and is NOT nested inside the disclosure toggle, so dismissing can't toggle the expand.
    expect(dismissBtn().closest(".chat-scheduled-chip-toggle")).toBeNull();
  });

  it("expands on click to show the full summary, the fire time, and View full result", () => {
    render(scheduledMessage());
    act(() => toggle().click());
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    const b = body()!;
    expect(b).toBeTruthy();
    // The WHOLE summary is shown, not the truncated one-liner.
    expect(b.querySelector(".chat-scheduled-summary")?.textContent).toBe(SUMMARY);
    // The fire time is rendered in the body (local time of 2026-11-03T14:00Z).
    const time = b.querySelector(".chat-scheduled-time")?.textContent ?? "";
    expect(time).toMatch(/\d{1,2}:\d{2}/);
    expect(viewFullResult()).toBeTruthy();
  });

  it("View full result opens the document viewer on this fire's turn", () => {
    render(scheduledMessage());
    act(() => toggle().click());
    act(() => viewFullResult()!.click());
    const state = useDocViewer.getState();
    expect(state.open).toBe(true);
    expect(state.doc?.title).toContain("brand-plan-sweep");
    expect(state.doc?.subtitle).toContain("Fired");
  });

  it("collapses again on a second click", () => {
    render(scheduledMessage());
    act(() => toggle().click());
    expect(body()).not.toBeNull();
    act(() => toggle().click());
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(body()).toBeNull();
  });
});

describe("ScheduledChip — keyboard operable (#4726)", () => {
  it("Enter toggles the expand and aria-expanded reflects the state", () => {
    render(scheduledMessage());
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    clickKey(toggle(), "Enter");
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect(body()).not.toBeNull();
    clickKey(toggle(), "Enter");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(body()).toBeNull();
  });

  it("Space also toggles the expand", () => {
    render(scheduledMessage());
    clickKey(toggle(), " ");
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect(body()).not.toBeNull();
    clickKey(toggle(), " ");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
  });
});

describe("ScheduledChip — dismiss works in both states without toggling expand (#4726)", () => {
  it("dismisses from the collapsed chip", () => {
    render(scheduledMessage());
    expect(toggle()).toBeTruthy();
    act(() => dismissBtn().click());
    // The chip is gone from this transcript entirely — not merely collapsed.
    expect(container.querySelector(".chat-scheduled-chip")).toBeNull();
  });

  it("dismisses from the expanded chip (dismiss is not the toggle)", () => {
    render(scheduledMessage());
    act(() => toggle().click());
    expect(body()).not.toBeNull();
    act(() => dismissBtn().click());
    expect(container.querySelector(".chat-scheduled-chip")).toBeNull();
  });
});

describe("ScheduledChip — failed fires still expand (#4726)", () => {
  it("says 'failed' and still opens the result body on click", () => {
    render(scheduledMessage({ status: "failed" }));
    expect(toggle().textContent).toContain("Scheduled task brand-plan-sweep failed");
    expect(container.querySelector(".chat-scheduled-chip--failed")).toBeTruthy();
    act(() => toggle().click());
    expect(body()?.querySelector(".chat-scheduled-summary")?.textContent).toBe(SUMMARY);
  });
});

describe("scheduled result markdown (#4927)", () => {
  const markdown = "**Failing CI:**\n\n- Fix the build\n- Retry tests";

  it.each([false, true])("renders markdown in the result body (collapse=%s)", async (collapse) => {
    await act(async () => render(scheduledMessage({ summary: markdown, collapse })));
    if (collapse) {
      expect(toggle().textContent).toContain("Failing CI: Fix the build Retry tests");
      expect(toggle().textContent).not.toMatch(/\*\*|(?:^|\s)- /);
      await act(async () => toggle().click());
    }
    const summary = container.querySelector(".chat-scheduled-summary")!;
    expect(summary.querySelector('[data-streamdown="strong"]')?.textContent).toBe("Failing CI:");
    expect([...summary.querySelectorAll("li")].map((li) => li.textContent)).toEqual([
      "Fix the build", "Retry tests",
    ]);
    expect(summary.textContent).not.toMatch(/\*\*|(?:^|\s)- /);
    act(() => viewFullResult()!.click());
    await expect(useDocViewer.getState().doc?.load?.()).resolves.toBe(markdown);
  });

  it.each([false, true])("keeps the empty summary fallback (collapse=%s)", async (collapse) => {
    await act(async () => render(scheduledMessage({ summary: "", collapse })));
    if (collapse) await act(async () => toggle().click());
    expect(container.querySelector(".chat-scheduled-summary")?.textContent).toBe("(no output)");
    act(() => viewFullResult()!.click());
    expect(useDocViewer.getState().open).toBe(true);
  });

  it.each([false, true])("preserves a plain summary (collapse=%s)", async (collapse) => {
    await act(async () => render(scheduledMessage({ collapse })));
    if (collapse) await act(async () => toggle().click());
    expect(container.querySelector(".chat-scheduled-summary")?.textContent).toBe(SUMMARY);
  });

  it("strips markup before applying the chip's 90-character limit", () => {
    render(scheduledMessage({ summary: "**" + "x".repeat(91) + "**" }));
    expect(toggle().textContent).toBe("Scheduled task brand-plan-sweep ran — " + "x".repeat(90) + "…");
  });

  it("strips common markdown without flattening lines", () => {
    expect(plainPreviewText("# Heading\n> **bold** __strong__ *italic* _emphasis_ `code`\n- [link](https://example.com)\n* item\n1. numbered"))
      .toBe("Heading\nbold strong italic emphasis code\nlink\nitem\nnumbered");
    expect(plainPreviewText("plain snake_case and 2 * 3")).toBe("plain snake_case and 2 * 3");
    expect(plainPreviewText("")).toBe("");
  });

  it("strips markdown before flattening and truncating server previews", () => {
    expect(serverResultPreview(markdown)).toBe("Failing CI: Fix the build Retry tests");
    expect(serverResultPreview("## Summary\n" + markdown + "\n## Details\nHidden"))
      .toBe("Failing CI: Fix the build Retry tests");
    expect(serverResultPreview("**" + "x".repeat(121) + "**")).toBe("x".repeat(120) + "…");
    expect(serverResultPreview("**abcdef**", 3)).toBe("abc…");
  });
});
