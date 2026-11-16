import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConversationReader } from "./ConversationReader";
import { ConversationsPane } from "./ConversationsPane";
import type { ConversationRow } from "./conversations-store";
import * as store from "./conversations-store";

// The read-only thread view inside the Conversations tab (docs/design/other-conversations.md,
// slice C3): header band + labelled turns + the read-only footer, with the needs-you / running /
// gone / failed states, and Back to list returning focus to the row it came from.

const mocks = vi.hoisted(() => {
  // A stand-in for the real `ApiError` so `err instanceof ApiError` in the reader matches — the
  // reader imports it from the same mocked "../lib/api" module, so it's the same class object.
  class ApiError extends Error {
    status: number;
    code?: string;
    constructor(status: number, message: string, code?: string) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
    }
  }
  return {
    ApiError,
    chatSession: vi.fn(),
    chatSessionTurns: vi.fn(),
    listConversations: vi.fn(),
    openWorkGroup: vi.fn(),
    continueConversation: vi.fn(),
    listeners: {} as Record<string, (d: Record<string, unknown>) => void>,
  };
});

vi.mock("../lib/api", () => ({
  api: {
    chatSession: mocks.chatSession,
    chatSessionTurns: mocks.chatSessionTurns,
    listConversations: mocks.listConversations,
  },
  ApiError: mocks.ApiError,
}));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (d: Record<string, unknown>) => void) => {
    mocks.listeners[topic] = fn;
    return () => delete mocks.listeners[topic];
  },
  onConnectionChange: (fn: (c: boolean) => void) => {
    fn(true);
    return () => {};
  },
}));
// Keep the "Answer in Needs you" navigation a seam and the prose synchronous.
vi.mock("./ChatStatusRail", () => ({ openWorkGroup: mocks.openWorkGroup }));
// ConversationsPane wires Continue → chatStore.continueConversation; stub it so the pane test
// asserts the call without pulling the whole chat-store module graph into this suite.
vi.mock("./chat-store", () => ({ chatStore: { continueConversation: mocks.continueConversation } }));
vi.mock("./Markdown", () => ({ Markdown: ({ children }: { children: string }) => h("p", null, children) }));
vi.mock("../work/tone", () => ({ relTime: () => ({ text: "2 min ago", tooltip: "12 Nov at 22:54" }) }));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

const row = (over: Partial<ConversationRow> = {}): ConversationRow => ({
  session_id: "ctx-1",
  title: "A delegation",
  origin_label: "From Claude Code",
  group: "for_you",
  last_updated: "2026-11-12T22:54:00Z",
  turn_count: 2,
  active: false,
  last_state: "completed",
  ...over,
});

const turn = (over: Record<string, unknown> = {}) => ({
  task_id: "t1",
  state: "completed",
  last_updated: "2026-11-12T22:54:00Z",
  text: "The agent reply.",
  history: [{ role: "user", parts: [{ kind: "text", text: "The caller question." }] }],
  ...over,
});

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  mocks.chatSession.mockResolvedValue({ session_id: "ctx-1", active: false, turn_count: 2, last_updated: null, last_state: "completed" });
  mocks.chatSessionTurns.mockResolvedValue({ turns: [turn()] });
  mocks.listConversations.mockResolvedValue({ conversations: [] });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  store.__resetForTest();
  for (const k of Object.keys(mocks.listeners)) delete mocks.listeners[k];
  vi.clearAllMocks();
});

async function renderReader(props: Partial<Parameters<typeof ConversationReader>[0]> = {}) {
  await act(async () => root.render(h(ConversationReader, { threadId: "ctx-1", row: row(), agentName: "designSystem", ...props })));
  await flush();
}

describe("ConversationReader header, turns and footer", () => {
  it("shows ← Back, the focused title, the status badge, a slim meta line, no eyebrow, and labels each message with its speaker (r1, r3)", async () => {
    mocks.chatSessionTurns.mockResolvedValue({ turns: [turn()] });
    await renderReader({ row: row({ active: true, last_state: "working", title: "Release triage" }) });

    // The reader names itself read-only in its accessible name; the eyebrow band is gone (C9c).
    expect(host.querySelector(".conversation-reader")?.getAttribute("aria-label")).toBe(
      "Conversation, read-only",
    );
    expect(host.textContent).not.toContain("Conversation · read-only");

    const back = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === "← Back");
    expect(back).toBeTruthy();

    const title = host.querySelector(".conversation-reader__title")!;
    expect(title.textContent).toBe("Release triage");
    expect((title as HTMLElement).tabIndex).toBe(-1);
    expect(document.activeElement).toBe(title);

    expect(host.querySelector(".pl-badge")?.textContent).toBe("Running");
    // The meta line shows the origin WITHOUT the leading "From " and the count (turn_count 2).
    const metaEl = host.querySelector(".conversation-reader__origin")!;
    expect(metaEl.textContent).toContain("Claude Code");
    expect(metaEl.textContent).toContain("2 messages");
    expect(metaEl.textContent?.startsWith("From")).toBe(false);

    const speakers = [...host.querySelectorAll(".pl-message__origin-label")].map((e) => e.textContent);
    expect(speakers).toContain("Claude Code"); // the origin caller
    expect(speakers).toContain("designSystem"); // the agent
    expect(host.textContent).toContain("The caller question.");
    expect(host.textContent).toContain("The agent reply.");
  });

  it("opens with title focus and no tooltip, but reveals the full title on hover and keyboard focus", async () => {
    const fullTitle = "From Claude Code: Ship the release";
    await renderReader({ row: row({ title: fullTitle, caller: "Claude Code" }) });
    const heading = host.querySelector<HTMLHeadingElement>("h2")!;
    expect(document.activeElement).toBe(heading);
    expect(heading.textContent).toBe("Ship the release");
    expect(document.querySelector('[role="tooltip"]')).toBeNull();

    const trigger = heading.querySelector<HTMLElement>(".pl-tip-wrap")!;
    vi.useFakeTimers();
    try {
      await act(async () => {
        trigger.dispatchEvent(new MouseEvent("pointermove", { bubbles: true }));
        await vi.advanceTimersByTimeAsync(250);
      });
      expect(document.querySelector('[role="tooltip"]')?.textContent).toBe(fullTitle);
      await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
      await act(async () => vi.runOnlyPendingTimersAsync());
      expect(document.querySelector('[role="tooltip"]')).toBeNull();
      const keyboardTarget = heading.querySelector<HTMLElement>('[tabindex="0"]')!;
      await act(async () => keyboardTarget.focus());
      expect(document.querySelector('[role="tooltip"]')?.textContent).toBe(fullTitle);
    } finally {
      vi.useRealTimers();
    }
  });

  it("Back shows '← Back' but reads and hovers as 'Back to From others' (r2)", async () => {
    await renderReader();
    const back = [...host.querySelectorAll("button")].find(
      (b) => b.textContent?.trim() === "← Back",
    ) as HTMLButtonElement;
    expect(back.getAttribute("aria-label")).toBe("Back to From others");
    expect(back.getAttribute("title")).toBe("Back to From others");
  });

  it("the h2 shows displayTitle() while onContinue keeps the full stored title (r4)", async () => {
    const onContinue = vi.fn();
    await renderReader({
      onContinue,
      row: row({
        title: "From Claude Code (Josh's session): Ship the release",
        origin_label: "From Claude Code for Josh",
        caller: "Claude Code",
        on_behalf_of: "Josh",
      }),
    });
    // The visible h2 is the stripped, capitalised remainder…
    expect(host.querySelector(".conversation-reader__title")?.textContent).toBe("Ship the release");
    // …but Continue in a new chat still forwards the FULL stored title (and raw origin).
    const cont = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Continue in a new chat"))!;
    await act(async () => cont.click());
    expect(onContinue).toHaveBeenCalledWith({
      contextId: "ctx-1",
      title: "From Claude Code (Josh's session): Ship the release",
      origin: "From Claude Code for Josh",
    });
  });

  it("meta line drops the leading 'From ' and hides the count at a single message (r5)", async () => {
    // A null timestamp keeps this timezone-independent (no date part to format).
    await renderReader({ row: row({ origin_label: "From another agent", last_updated: null, turn_count: 1 }) });
    expect(host.querySelector(".conversation-reader__origin")?.textContent).toBe("another agent");
  });

  it("has no composer; its slot is one row — the lock + the short read-only clause and the two chat-forwarding actions (r1, r2)", async () => {
    await renderReader();
    expect(host.querySelector("textarea")).toBeNull();
    expect(host.querySelector("input")).toBeNull();

    // The footer is one row: the lede (lock + short clause) on the left, the two actions on the
    // right. The long sentence is NOT visible text here — it lives on the lock's tooltip.
    const footer = host.querySelector(".conversation-reader__footer")!;
    expect(footer.querySelector(".conversation-reader__footer-lede")).not.toBeNull();
    expect(footer.querySelector(".conversation-reader__footer-actions")).not.toBeNull();
    expect(host.querySelector(".conversation-reader__footer-note")?.textContent).toBe(
      "Read-only. Replies start a new chat.",
    );
    expect(host.textContent).not.toContain("Replies go to a new chat, so Claude Code");

    const buttons = [...host.querySelectorAll("button")].map((b) => b.textContent ?? "");
    expect(buttons.some((t) => t.includes("Continue in a new chat"))).toBe(true);
    expect(buttons.some((t) => t.includes("Copy link"))).toBe(true);
  });

  it("the footer lock is a focusable tooltip trigger whose accessible name is the full sentence (r2)", async () => {
    await renderReader({ row: row({ origin_label: "From Claude Code" }) });
    const lock = host.querySelector<HTMLElement>(".conversation-reader__footer-lock")!;
    // Focusable (keyboard reaches it) and named by the full sentence…
    expect(lock.tabIndex).toBe(0);
    const name = lock.getAttribute("aria-label") ?? "";
    expect(name).toContain("You can");
    expect(name).toContain("Replies go to a new chat, so Claude Code");
    expect(name).toContain("interrupted.");
    // …and it is wrapped by a DS Tooltip trigger (its label carries the same sentence).
    expect(lock.closest(".pl-tip-wrap")).not.toBeNull();
  });

  it("Copy link keeps its accessible name whether or not its visible label is shown (r2, r3)", async () => {
    await renderReader();
    const copy = [...host.querySelectorAll("button")].find((b) =>
      b.getAttribute("aria-label") === "Copy link",
    ) as HTMLButtonElement;
    // The accessible name is set explicitly, so the phone's icon-only form (label hidden by CSS)
    // keeps the same name a screen reader reads on desktop.
    expect(copy).toBeTruthy();
    expect(copy.querySelector(".conversation-reader__footer-copy-label")?.textContent).toBe("Copy link");
  });

  it("Continue in a new chat hands the thread's id, title and origin to onContinue (C4, r2)", async () => {
    const onContinue = vi.fn();
    await renderReader({ onContinue, threadId: "ctx-1", row: row({ title: "A delegation", origin_label: "From Claude Code" }) });
    const cont = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Continue in a new chat"))!;
    await act(async () => cont.click());
    expect(onContinue).toHaveBeenCalledTimes(1);
    expect(onContinue).toHaveBeenCalledWith({ contextId: "ctx-1", title: "A delegation", origin: "From Claude Code" });
  });

  it("Copy link copies the thread's hash route (r2)", async () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await renderReader();
    const copy = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Copy link"))!;
    await act(async () => copy.click());
    expect(writeText).toHaveBeenCalledWith("#/conversations/ctx-1");
  });
});

describe("ConversationReader states", () => {
  it("needs you: warning callout + Answer in Needs you, which opens the group in Work (r3)", async () => {
    await renderReader({ row: row({ last_state: "input-required", active: false }) });
    expect(host.textContent).toContain("This conversation is waiting for your answer.");
    expect(host.querySelector(".pl-badge")?.textContent).toBe("Needs you");

    const answer = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Answer in Needs you"))!;
    await act(async () => answer.click());
    expect(mocks.openWorkGroup).toHaveBeenCalledWith("needs-you");
  });

  it("running shows a typing indicator as the last message; idle shows none (r3)", async () => {
    await renderReader({ row: row({ active: true, last_state: "working" }) });
    expect(host.querySelector(".pl-typing")).not.toBeNull();

    await renderReader({ row: row({ active: false, last_state: "completed" }) });
    expect(host.querySelector(".pl-typing")).toBeNull();
  });

  it("gone: a 404 renders the no-longer-stored empty state with Back to list and no footer (r3)", async () => {
    mocks.chatSession.mockRejectedValue(new mocks.ApiError(404, "unknown session"));
    await renderReader();
    expect(host.textContent).toContain("This conversation is no longer stored.");
    expect(host.textContent).toContain("has been cleared");
    expect([...host.querySelectorAll("button")].some((b) => b.textContent === "Back to list")).toBe(true);
    // The composer-slot note is gone on this dead end.
    expect(host.textContent).not.toContain("Continue in a new chat");
    // Never fetched the body once the thread turned out to be gone.
    expect(mocks.chatSessionTurns).not.toHaveBeenCalled();
  });

  it("failed: a non-404 error renders the error alert, and Try again recovers (r3)", async () => {
    mocks.chatSession.mockRejectedValue(new Error("network down"));
    await renderReader();
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
    expect(host.textContent).toContain("load this conversation");

    // Recover on retry.
    mocks.chatSession.mockResolvedValue({ session_id: "ctx-1", active: false, turn_count: 2, last_updated: null, last_state: "completed" });
    const tryAgain = [...host.querySelectorAll("button")].find((b) => b.textContent === "Try again")!;
    await act(async () => tryAgain.click());
    await flush();
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(host.textContent).toContain("The agent reply.");
  });
});

describe("ConversationsPane opening, remembering and returning to a thread (r4)", () => {
  async function renderPane() {
    await act(async () => root.render(h(ConversationsPane, { agentName: "designSystem" })));
    await flush();
  }

  it("Back to list returns focus to the row the thread was opened from", async () => {
    mocks.listConversations.mockResolvedValue({ conversations: [row({ session_id: "ctx-1" })] });
    await renderPane();

    const rowBtn = host.querySelector<HTMLButtonElement>('.conversations-row__btn[data-row-id="ctx-1"]')!;
    await act(async () => rowBtn.click());
    await flush();
    expect(host.querySelector(".conversation-reader")).not.toBeNull();
    expect(document.activeElement).toBe(host.querySelector("h2"));
    expect(document.querySelector('[role="tooltip"]')).toBeNull();

    const back = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === "← Back")!;
    await act(async () => back.click());
    await flush();

    const refocused = host.querySelector<HTMLButtonElement>('.conversations-row__btn[data-row-id="ctx-1"]')!;
    expect(host.querySelector(".conversation-reader")).toBeNull();
    expect(document.activeElement).toBe(refocused);
  });

  it("remembers the open thread across a tab switch (leave for a chat, return to the tab)", async () => {
    mocks.listConversations.mockResolvedValue({ conversations: [row({ session_id: "ctx-1", title: "A delegation" })] });
    await renderPane();

    await act(async () => host.querySelector<HTMLButtonElement>('.conversations-row__btn[data-row-id="ctx-1"]')!.click());
    await flush();
    expect(host.querySelector(".conversation-reader__title")?.textContent).toBe("A delegation");

    // Selecting a chat tab leaves the Conversations stage; the pane unmounts.
    await act(async () => store.openChat());
    await act(async () => root.render(h("div", null)));

    // Returning to the tab remounts the pane — the store still holds the open thread.
    await act(async () => root.render(h(ConversationsPane, { agentName: "designSystem" })));
    await flush();
    expect(host.querySelector(".conversations-list")).toBeNull();
    expect(host.querySelector(".conversation-reader__title")?.textContent).toBe("A delegation");
  });

  it("Continue in a new chat drives chatStore.continueConversation with the open thread (r3)", async () => {
    mocks.listConversations.mockResolvedValue({
      conversations: [row({ session_id: "ctx-1", title: "A delegation", origin_label: "From Claude Code" })],
    });
    await renderPane();

    await act(async () => host.querySelector<HTMLButtonElement>('.conversations-row__btn[data-row-id="ctx-1"]')!.click());
    await flush();
    const cont = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Continue in a new chat"))!;
    await act(async () => cont.click());

    expect(mocks.continueConversation).toHaveBeenCalledWith({
      contextId: "ctx-1",
      title: "A delegation",
      origin: "From Claude Code",
    });
  });
});
