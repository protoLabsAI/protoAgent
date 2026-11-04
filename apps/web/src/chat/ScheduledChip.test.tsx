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

beforeEach(() => {
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
