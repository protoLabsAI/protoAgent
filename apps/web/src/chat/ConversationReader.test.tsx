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
  it("shows Back to list, the read-only eyebrow, the focused title, the status badge, the origin line, and labels each message with its speaker (r1)", async () => {
    mocks.chatSessionTurns.mockResolvedValue({ turns: [turn()] });
    await renderReader({ row: row({ active: true, last_state: "working", title: "Release triage" }) });

    const buttons = [...host.querySelectorAll("button")].map((b) => b.textContent ?? "");
    expect(buttons.some((t) => t.includes("Back to list"))).toBe(true);
    expect(host.textContent).toContain("Conversation · read-only");

    const title = host.querySelector(".conversation-reader__title")!;
    expect(title.textContent).toBe("Release triage");
    expect((title as HTMLElement).tabIndex).toBe(-1);
    expect(document.activeElement).toBe(title);

    expect(host.querySelector(".pl-badge")?.textContent).toBe("Running");
    expect(host.textContent).toContain("From Claude Code");

    const speakers = [...host.querySelectorAll(".pl-message__origin-label")].map((e) => e.textContent);
    expect(speakers).toContain("Claude Code"); // the origin caller
    expect(speakers).toContain("designSystem"); // the agent
    expect(host.textContent).toContain("The caller question.");
    expect(host.textContent).toContain("The agent reply.");
  });

  it("has no composer; its slot carries the read-only note and the two chat-forwarding actions (r2)", async () => {
    await renderReader();
    expect(host.querySelector("textarea")).toBeNull();
    expect(host.querySelector("input")).toBeNull();
    expect(host.textContent).toContain("Replies go to a new chat, so Claude Code");

    const buttons = [...host.querySelectorAll("button")].map((b) => b.textContent ?? "");
    expect(buttons.some((t) => t.includes("Continue in a new chat"))).toBe(true);
    expect(buttons.some((t) => t.includes("Copy link"))).toBe(true);
  });

  it("Continue in a new chat calls the onContinue seam (C4 wires it) (r2)", async () => {
    const onContinue = vi.fn();
    await renderReader({ onContinue });
    const cont = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Continue in a new chat"))!;
    await act(async () => cont.click());
    expect(onContinue).toHaveBeenCalledTimes(1);
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

    const back = [...host.querySelectorAll("button")].find((b) => b.textContent?.includes("Back to list"))!;
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
});
