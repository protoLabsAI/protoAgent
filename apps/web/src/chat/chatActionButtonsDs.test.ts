// Render-level proof for #551 (action-button rule, card 2): the chat's hand-rolled action
// controls — the three report/scheduled dismiss ✕ buttons, the delegation "Show/Hide brief"
// toggle, and the background-work "View" button — now render through the DS `Button`
// primitive (`pl-btn`) with the card's variant/size/icon, keeping their handlers, aria-label
// and aria-expanded. The retired hand-rolled classes (.chat-report-dismiss,
// .chat-delegation-toggle, .chat-bgwork-open) must be gone from both the DOM and chat.css.
// (Same jsdom mount pattern as streamingIndicatorRender.test.ts; the background-store /
//  bus mocks mirror backgroundJobStore.test.ts so the store hydrate stays offline.)
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import chatCss from "./chat.css?raw";
import type { ChatMessage } from "../lib/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Bus topic handlers the background store registers on subscribe — captured so a case can
// seed a "running" job exactly as a real `background.started` event would.
const topicHandlers: Record<string, (d: Record<string, unknown>) => void> = {};

vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (d: Record<string, unknown>) => void) => {
    topicHandlers[topic] = fn;
    return () => delete topicHandlers[topic];
  },
  onConnectionChange: (fn: (c: boolean) => void) => {
    fn(true);
    return () => {};
  },
}));

// Keep the real api surface (loadBackgroundReport et al.) but neutralise the network the
// background store fires on hydrate, so these render tests stay offline and deterministic.
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      background: () => Promise.resolve({ enabled: true, jobs: [] }),
      backgroundJob: () => Promise.reject(new Error("not used in this test")),
    },
  };
});

const { ChatMessageView } = await import("./ChatMessageView");
const { BackgroundWorkStrip } = await import("./BackgroundWorkStrip");
const { __resetForTest } = await import("./backgroundJobStore");
const { useUI } = await import("../state/uiStore");

let root: Root | null = null;
let host: HTMLElement | null = null;

async function mount(node: ReactElement): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(node);
  });
  return host;
}

beforeEach(() => {
  __resetForTest();
  for (const k of Object.keys(topicHandlers)) delete topicHandlers[k];
  localStorage.clear();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function msg(over: Partial<ChatMessage>): ChatMessage {
  return { id: "m1", role: "assistant", content: "", ...over };
}

const btnByText = (el: HTMLElement, text: string) =>
  [...el.querySelectorAll("button")].find((b) => (b.textContent ?? "").trim() === text);

/** A DS Button with the given ghost variant/size (and optionally icon-only) — asserted by the
 *  `pl-btn` class chain the primitive emits, which is what proves the swap actually happened. */
function expectGhostButton(btn: Element | null | undefined, size: "xs" | "sm", opts: { icon?: boolean } = {}) {
  expect(btn).toBeTruthy();
  const cl = (btn as HTMLElement).classList;
  expect(cl.contains("pl-btn")).toBe(true);
  expect(cl.contains("pl-btn--ghost")).toBe(true);
  expect(cl.contains(`pl-btn--${size}`)).toBe(true);
  expect(cl.contains("pl-btn--icon")).toBe(opts.icon === true);
}

describe("chat action buttons → DS Button (#551 card 2)", () => {
  it("background-report dismiss ✕ is a ghost/xs/icon DS Button that keeps its label + handler", async () => {
    const el = await mount(createElement(ChatMessageView, { message: msg({ role: "system", report: { jobId: "bg-1", title: "Nightly digest" } }) }));
    const btn = el.querySelector<HTMLButtonElement>('[aria-label="Dismiss report"]');
    expectGhostButton(btn, "xs", { icon: true });
    // The X glyph and the descriptive title survive the swap.
    expect(btn!.querySelector("svg")).toBeTruthy();
    expect(btn!.getAttribute("title")).toContain("Background agents panel");
    expect(el.querySelector(".chat-report-dismiss")).toBeNull();
    // onClick still dismisses the chip (the whole card returns null once dismissed).
    await act(async () => btn!.click());
    expect(el.querySelector('[aria-label="Dismiss report"]')).toBeNull();
  });

  it("scheduled RESULT-card dismiss ✕ is a ghost/xs/icon DS Button", async () => {
    const el = await mount(
      createElement(ChatMessageView, {
        message: msg({
          role: "system",
          scheduled: { jobId: "j1", firedAt: "2026-01-01T14:00:00Z", summary: "did the thing", status: "completed" },
        }),
      }),
    );
    const btn = el.querySelector<HTMLButtonElement>('[aria-label="Dismiss scheduled result"]');
    expectGhostButton(btn, "xs", { icon: true });
    expect(btn!.querySelector("svg")).toBeTruthy();
    expect(el.querySelector(".chat-report-dismiss")).toBeNull();
    await act(async () => btn!.click());
    expect(el.querySelector('[aria-label="Dismiss scheduled result"]')).toBeNull();
  });

  it("recurring scheduled CHIP dismiss ✕ is a ghost/xs/icon DS Button", async () => {
    const el = await mount(
      createElement(ChatMessageView, {
        message: msg({
          role: "system",
          scheduled: { jobId: "j2", firedAt: "2026-01-01T15:00:00Z", summary: "ran again", status: "completed", collapse: true },
        }),
      }),
    );
    const btn = el.querySelector<HTMLButtonElement>('[aria-label="Dismiss scheduled result"]');
    expectGhostButton(btn, "xs", { icon: true });
    expect(el.querySelector(".chat-report-dismiss")).toBeNull();
  });

  it("delegation 'Show brief' toggle is a ghost/sm DS Button that keeps aria-expanded + toggles the brief", async () => {
    const el = await mount(
      createElement(ChatMessageView, { message: msg({ role: "assistant", addressedTo: "sonnet", content: "Please land PR #13. Then report back." }) }),
    );
    const toggle = btnByText(el, "Show brief");
    expectGhostButton(toggle, "sm");
    expect(toggle!.getAttribute("aria-expanded")).toBe("false");
    expect(el.querySelector(".chat-delegation-toggle")).toBeNull();
    expect(el.querySelector(".chat-delegation-brief")).toBeNull();
    // Same handler: clicking flips the label, aria-expanded and reveals the brief.
    await act(async () => toggle!.click());
    const opened = btnByText(el, "Hide brief");
    expect(opened).toBeTruthy();
    expect(opened!.getAttribute("aria-expanded")).toBe("true");
    expect(el.querySelector(".chat-delegation-brief")).toBeTruthy();
  });

  it("background-work strip 'View' is a ghost/sm DS Button that keeps its openBackgroundJobs handler", async () => {
    // Mount first (this subscribes the store and registers the bus handlers), then seed a
    // running job for this session so the strip renders.
    const el = await mount(createElement(BackgroundWorkStrip, { sessionId: "sess-1" }));
    await act(async () => {
      topicHandlers["background.started"]?.({
        job_id: "bg-x",
        subagent_type: "delegate",
        description: "delegate → sonnet: Do it",
        origin_session: "sess-1",
      });
    });
    const view = btnByText(el, "View");
    expectGhostButton(view, "sm");
    expect(el.querySelector(".chat-bgwork-open")).toBeNull();
    // onClick still asks the UI store to open the Background agents panel.
    const before = useUI.getState().backgroundJobsRequest;
    await act(async () => view!.click());
    expect(useUI.getState().backgroundJobsRequest).toBe(before + 1);
  });

  it("the retired hand-rolled control classes are gone from chat.css", () => {
    expect(chatCss.length).toBeGreaterThan(100); // guard: real stylesheet text, not an empty stub
    expect(chatCss).not.toContain("chat-report-dismiss");
    expect(chatCss).not.toContain("chat-delegation-toggle");
    expect(chatCss).not.toContain("chat-bgwork-open");
  });
});
