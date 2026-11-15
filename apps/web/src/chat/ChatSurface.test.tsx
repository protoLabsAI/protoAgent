import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { chatStore, type ChatSession, type SessionStatus } from "./chat-store";
import { retireChatSession, NO_MEMORY_CHANGE } from "./sessionRetirement";
import { noteTurnStarted, resetServerTurns } from "./server-turn-store";
import { openContextMenu } from "../contextMenu";

import type { ChatMessage } from "../lib/types";
import {
  ChatSurface,
  canSubmitChatDraft,
  chatComposerBusy,
  resolveComposerStopTarget,
  serverTurnRestoreAfterFailedCancel,
} from "./ChatSurface";
import { isDuplicateRiskActive, nextDuplicateRisk, type DuplicateRisk } from "./duplicateRisk";
import * as conversationsStore from "./conversations-store";
import type { ConversationRow } from "./conversations-store";

describe("ChatSurface server-turn controls", () => {
  const control = { taskId: "task-server" };

  it("renders the composer as busy for attended server turns so Stop and queue affordances show", () => {
    expect(chatComposerBusy("idle", "running a scheduled task…", control)).toBe(true);
    expect(chatComposerBusy("idle", null, null)).toBe(false);
    expect(chatComposerBusy("streaming", null, null)).toBe(true);
  });

  it("leaves the composer non-busy for an UNATTENDED server turn (base behavior preserved)", () => {
    // serverTurnLabel is set but there is no controllable task — the composer must stay
    // fully normal so a plain send still starts a browser-owned turn, exactly as before.
    expect(chatComposerBusy("idle", "running a scheduled task…", null)).toBe(false);
  });

  it("allows text interjections for attended server turns without allowing attachment-only sends", () => {
    expect(
      canSubmitChatDraft({
        draft: "please prioritize the inbox item",
        hasReadyAttachment: false,
        status: "idle",
        signedOut: false,
        serverTurnLabel: "running a scheduled task…",
        serverTurnControl: control,
      }),
    ).toBe(true);
    expect(
      canSubmitChatDraft({
        draft: "",
        hasReadyAttachment: true,
        status: "idle",
        signedOut: false,
        serverTurnLabel: "running a scheduled task…",
        serverTurnControl: control,
      }),
    ).toBe(false);
  });

  it("keeps unattended server turns on the ordinary send path (no control, normal send allowed)", () => {
    // No control frame → the composer is non-interactive w.r.t. the server turn (no Stop /
    // interjection), but a normal message with text still sends as its own browser turn.
    expect(
      canSubmitChatDraft({
        draft: "ask a fresh question",
        hasReadyAttachment: false,
        status: "idle",
        signedOut: false,
        serverTurnLabel: "running a scheduled task…",
        serverTurnControl: null,
      }),
    ).toBe(true);
    // …and an attachment-only send is still allowed on that ordinary path.
    expect(
      canSubmitChatDraft({
        draft: "",
        hasReadyAttachment: true,
        status: "idle",
        signedOut: false,
        serverTurnLabel: "running a scheduled task…",
        serverTurnControl: null,
      }),
    ).toBe(true);
  });

  it("preserves existing streaming send gating", () => {
    expect(
      canSubmitChatDraft({
        draft: "steer this",
        hasReadyAttachment: false,
        status: "streaming",
        signedOut: false,
        serverTurnLabel: null,
        serverTurnControl: null,
      }),
    ).toBe(false);
  });

  it("prefers the server-provided durable task id for Stop", () => {
    const messages: ChatMessage[] = [
      { id: "a1", role: "assistant", content: "working", status: "streaming", taskId: "task-local" },
    ];

    expect(resolveComposerStopTarget(messages, "", control)).toBe("task-server");
    expect(resolveComposerStopTarget(messages, "", null)).toBe("task-local");
    expect(resolveComposerStopTarget(messages, "task-owned", control)).toBe("task-server");
  });
});

describe("Stop whose cancel RPC fails (#3092 review finding)", () => {
  // The board's review gate blocked PR #3350 on this and it was right: `stop()` clears
  // the server-turn control BEFORE awaiting api.cancelTask, so a rejected cancel left a
  // still-running server turn with no Stop or interjection affordance — the operator
  // locked out of the very turn they asked to stop, with no way to retry.
  const control = { taskId: "task-server" };

  it("puts the control back so the operator can retry the cancel", () => {
    const restore = serverTurnRestoreAfterFailedCancel(control, "running a scheduled task…");
    expect(restore).toEqual({ control, label: "running a scheduled task…" });
  });

  it("restores the control even when the label is already gone", () => {
    // noteTurnFinished may have dropped the label before the RPC rejected; the control is
    // what drives Stop/interjection, so it must come back regardless.
    expect(serverTurnRestoreAfterFailedCancel(control, null)).toEqual({ control, label: "" });
  });

  it("restores nothing for a locally-owned stream", () => {
    // No server control: the browser aborted its own stream client-side, so the turn is
    // correctly settled and there is no server-side task left to reach.
    expect(serverTurnRestoreAfterFailedCancel(null, "streaming…")).toBeNull();
    expect(serverTurnRestoreAfterFailedCancel(undefined, null)).toBeNull();
  });
});

describe("consumed ↑-recall duplicate-risk marker (#3413)", () => {
  const SID = "sess-1";
  const risk: DuplicateRisk = { sessionId: SID, text: "actually, do X" };

  describe("isDuplicateRiskActive", () => {
    it("is inactive when there is no marker (the removed:true path never sets one — r1)", () => {
      expect(isDuplicateRiskActive(null, SID, "")).toBe(false);
      expect(isDuplicateRiskActive(null, SID, "anything")).toBe(false);
    });

    it("is active while the marker's exact recalled text is still the draft in its session (r2)", () => {
      expect(isDuplicateRiskActive(risk, SID, "actually, do X")).toBe(true);
    });

    it("goes inactive the moment the draft is edited into a follow-up or cleared (r3, r5)", () => {
      expect(isDuplicateRiskActive(risk, SID, "actually, do X instead")).toBe(false);
      expect(isDuplicateRiskActive(risk, SID, "")).toBe(false);
    });

    it("never leaks onto another session's composer — it cannot survive a session switch (r5)", () => {
      expect(isDuplicateRiskActive(risk, "other-session", "actually, do X")).toBe(false);
      expect(isDuplicateRiskActive(risk, null, "actually, do X")).toBe(false);
    });
  });

  describe("nextDuplicateRisk transitions", () => {
    it("pins a marker to the recalled text + session on a consumed recall (r2)", () => {
      expect(nextDuplicateRisk(null, { type: "recall-consumed", sessionId: SID, text: "hi there" })).toEqual({
        sessionId: SID,
        text: "hi there",
      });
    });

    it("drops the marker on a subsequent clean removed recall (r5)", () => {
      expect(nextDuplicateRisk(risk, { type: "recall-removed" })).toBeNull();
    });

    it("drops the marker on a deliberate send and on an explicit clear (r3, r4)", () => {
      expect(nextDuplicateRisk(risk, { type: "sent" })).toBeNull();
      expect(nextDuplicateRisk(risk, { type: "clear" })).toBeNull();
    });

    it("drops the marker when the draft diverges, is cleared, or the session changes (r3, r5)", () => {
      expect(nextDuplicateRisk(risk, { type: "draft", sessionId: SID, draft: "edited follow-up" })).toBeNull();
      expect(nextDuplicateRisk(risk, { type: "draft", sessionId: SID, draft: "" })).toBeNull();
      expect(nextDuplicateRisk(risk, { type: "draft", sessionId: "other", draft: "actually, do X" })).toBeNull();
    });

    it("keeps the SAME marker reference while the draft is still the recalled text (no render churn)", () => {
      // The clearing effect dispatches `draft` on every keystroke; returning the same object
      // while the marker still applies lets React bail out of the state update.
      expect(nextDuplicateRisk(risk, { type: "draft", sessionId: SID, draft: "actually, do X" })).toBe(risk);
      // And a no-op draft event against no marker stays null (also a bail-out).
      expect(nextDuplicateRisk(null, { type: "draft", sessionId: SID, draft: "typing…" })).toBeNull();
    });
  });
});

const closeHarness = vi.hoisted(() => ({
  refetch: vi.fn(),
  goals: [] as { session_id: string; status: string; condition: string }[],
}));
// The C1 conversations store is mocked so the tab/pane render off a controllable snapshot (no
// network, no bus). `state` is the exact object `useConversations()` returns; the mutators are
// spies. `def()` is the no-tab default, so every test that doesn't opt in (incl. the close-
// request suite above) sees the strip exactly as it is today.
const convHarness = vi.hoisted(() => {
  const def = () => ({
    status: "ready" as "idle" | "loading" | "ready" | "error",
    rows: [] as unknown[],
    hasOlder: false,
    loadingOlder: false,
    hidden: false,
    stage: "chat" as "chat" | "conversations",
    openThreadId: null as string | null,
    deepLink: false,
    forYou: [] as unknown[],
    automated: [] as unknown[],
    count: 0,
    running: 0,
    needsYou: 0,
    tabVisible: false,
    loadOlder: vi.fn(),
  });
  return { def, state: def() };
});
vi.mock("@tanstack/react-query", async (original) => ({
  ...await original<typeof import("@tanstack/react-query")>(),
  useQuery: () => ({ data: { goals: closeHarness.goals }, refetch: closeHarness.refetch }),
}));
vi.mock("./ChatSessionSlot", async (original) => ({
  ...await original<typeof import("./ChatSessionSlot")>(),
  ChatSessionSlot: () => null,
}));
vi.mock("./sessionRetirement", async (original) => ({
  ...await original<typeof import("./sessionRetirement")>(),
  retireChatSession: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("../contextMenu", () => ({ openContextMenu: vi.fn() }));
vi.mock("@protolabsai/ui/overlays", async (original) => ({
  ...await original<typeof import("@protolabsai/ui/overlays")>(),
  useToast: () => vi.fn(),
}));
vi.mock("./conversations-store", async (original) => ({
  ...await original<typeof import("./conversations-store")>(),
  useConversations: () => convHarness.state,
  hide: vi.fn(),
  show: vi.fn(),
  setStage: vi.fn(),
  openChat: vi.fn(),
  openThread: vi.fn(),
  retry: vi.fn(),
}));

describe("ChatSurface close requests", () => {
  let container: HTMLDivElement;
  let root: Root;
  let session: ChatSession;
  let snapshot: ReturnType<typeof chatStore.getSnapshot>;
  const onError = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    resetServerTurns();
    closeHarness.goals = [];
    closeHarness.refetch.mockImplementation(async () => ({
      error: null, data: { goals: closeHarness.goals },
    }));
    session = { id: "empty", title: "Empty chat", messages: [], createdAt: 1, updatedAt: 1 };
    snapshot = {
      ...chatStore.getSnapshot(), sessions: [session], currentSessionId: session.id,
      activeSessions: [], sessionStatusMap: {}, pendingDeleteRequest: null, pendingClearRequest: null,
    };
    vi.spyOn(chatStore, "getSnapshot").mockImplementation(() => snapshot);
    vi.spyOn(chatStore, "clearDeleteRequest").mockImplementation(() => {
      snapshot = { ...snapshot, pendingDeleteRequest: null };
    });
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false, addEventListener() {}, removeEventListener() {},
    });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    resetServerTurns();
    vi.restoreAllMocks();
  });

  async function mount() {
    await act(async () => root.render(h(ChatSurface, { onError })));
  }

  async function close(shiftKey = false) {
    await act(async () => {
      container.querySelector(".pl-tabbar__close")!.dispatchEvent(
        new MouseEvent("click", { bubbles: true, shiftKey }),
      );
    });
  }

  function expectDialog(title = "Delete this chat?") {
    expect(document.body.textContent).toContain(title);
    expect(retireChatSession).not.toHaveBeenCalled();
  }

  it.each([false, true])("closes empty history without a dialog (ephemeral=%s)", async (ephemeral) => {
    if (ephemeral) session.messages = [{ id: "notice", role: "assistant", content: "notice", ephemeral }];
    await mount();
    await close();
    expect(retireChatSession).toHaveBeenCalledWith(session.id, NO_MEMORY_CHANGE);
    expect(document.body.textContent).not.toContain("Delete this chat?");
  });

  it("confirms a conversation containing a message", async () => {
    session.messages = [{ id: "user", role: "user", content: "hello" }];
    await mount();
    await close();
    expectDialog();
    expect(closeHarness.refetch).not.toHaveBeenCalled();
  });

  it("confirms an empty tab's active goal after refreshing ownership", async () => {
    await mount();
    closeHarness.goals = [{ session_id: session.id, status: "active", condition: "Finish work" }];
    await close();
    expectDialog("Close this goal tab?");
  });

  it("keeps the tab when a failed refetch retains stale data", async () => {
    closeHarness.refetch.mockResolvedValue({ error: new Error("offline"), data: { goals: [] } });
    await mount();
    await close();
    expect(retireChatSession).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(
      "Couldn't verify whether this chat owns an active goal. The tab was kept; try again.",
    );
  });

  it.each(["foreground", "server"])("confirms an empty tab with a %s turn", async (source) => {
    if (source === "foreground") snapshot.sessionStatusMap[session.id] = "streaming" as SessionStatus;
    else noteTurnStarted(session.id, "working");
    await mount();
    await close();
    expectDialog();
  });

  it("routes the context menu Close through the empty-chat shortcut", async () => {
    await mount();
    await act(async () => {
      container.querySelector(".pl-tabbar__tab")!.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    });
    const context = vi.mocked(openContextMenu).mock.calls[0][2] as { onClose: () => void };
    await act(async () => context.onClose());
    expect(retireChatSession).toHaveBeenCalledWith(session.id, NO_MEMORY_CHANGE);
  });

  it("routes store delete requests through the empty-chat shortcut", async () => {
    snapshot.pendingDeleteRequest = session.id;
    await mount();
    expect(chatStore.clearDeleteRequest).toHaveBeenCalled();
    expect(retireChatSession).toHaveBeenCalledWith(session.id, NO_MEMORY_CHANGE);
  });

  it("preserves Shift+click quick-delete for non-empty chats", async () => {
    session.messages = [{ id: "user", role: "user", content: "hello" }];
    await mount();
    await close(true);
    expect(retireChatSession).toHaveBeenCalledWith(session.id, NO_MEMORY_CHANGE);
  });

  it.each(["removed", "message", "turn"])("rechecks the tab after awaiting ownership: %s", async (change) => {
    let resolve!: (value: unknown) => void;
    closeHarness.refetch.mockReturnValue(new Promise((done) => { resolve = done; }));
    await mount();
    await close();
    if (change === "removed") snapshot.sessions = [];
    else if (change === "message") session.messages = [{ id: "user", role: "user", content: "hello" }];
    else snapshot.sessionStatusMap[session.id] = "streaming";
    await act(async () => resolve({ error: null, data: { goals: [] } }));
    expect(retireChatSession).not.toHaveBeenCalled();
    if (change !== "removed") expectDialog();
  });
});

describe("ChatSurface Conversations tab (other conversations C2)", () => {
  let container: HTMLDivElement;
  let root: Root;
  const onError = vi.fn();

  const row = (over: Partial<ConversationRow> = {}): ConversationRow => ({
    session_id: "ctx-1",
    title: "Friction triage",
    origin_label: "From Claude Code for Josh",
    group: "for_you",
    last_updated: "2026-11-14T10:00:00Z",
    turn_count: 13,
    active: false,
    last_state: "completed",
    ...over,
  });

  function setConv(patch: Partial<typeof convHarness.state>) {
    convHarness.state = { ...convHarness.state, ...patch };
  }

  beforeEach(() => {
    vi.clearAllMocks();
    resetServerTurns();
    closeHarness.goals = [];
    convHarness.state = convHarness.def();
    const snapshot = {
      ...chatStore.getSnapshot(),
      sessions: [{ id: "chat-1", title: "My chat", messages: [], createdAt: 1, updatedAt: 1 }],
      currentSessionId: "chat-1",
      activeSessions: [],
      sessionStatusMap: {},
      pendingDeleteRequest: null,
      pendingClearRequest: null,
    };
    vi.spyOn(chatStore, "getSnapshot").mockImplementation(() => snapshot);
    vi.spyOn(chatStore, "switchSession").mockImplementation(() => {});
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false, addEventListener() {}, removeEventListener() {},
    });
    // Radix Tooltip (the rows' full-timestamp tip) mounts cleanly in jsdom with this stub.
    (window as unknown as { ResizeObserver?: unknown }).ResizeObserver ||= class { observe() {} unobserve() {} disconnect() {} };
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    resetServerTurns();
    convHarness.state = convHarness.def();
    vi.restoreAllMocks();
  });

  async function mount() {
    await act(async () => root.render(h(ChatSurface, { onError })));
  }
  const pinnedTab = () => container.querySelector(".pl-tabbar__tab--pinned") as HTMLElement | null;

  it.each([
    ["while the list is loading", { status: "loading" as const, tabVisible: false }],
    ["with zero rows in the window", { status: "ready" as const, count: 0, tabVisible: false }],
    ["after Hide with nothing needing you", { status: "ready" as const, count: 3, hidden: true, tabVisible: false }],
  ])("does not draw the tab %s (r1)", async (_label, patch) => {
    setConv(patch);
    await mount();
    expect(pinnedTab()).toBeNull();
  });

  it("draws the tab pinned first, with no close button, once rows have loaded (r1)", async () => {
    setConv({ tabVisible: true, count: 7, running: 1, rows: [row()], forYou: [row()] });
    await mount();
    const tabs = Array.from(container.querySelectorAll(".pl-tabbar__tab"));
    expect(pinnedTab()).toBe(tabs[0]); // pinned first in the strip (§3.1)
    expect(pinnedTab()!.textContent).toContain("From others");
    expect(pinnedTab()!.querySelector(".pl-tabbar__close")).toBeNull();
  });

  it.each([
    [1, "From others tab added: 1 thread from Claude Code for Josh."],
    [7, "From others tab added: 7 threads from Claude Code for Josh."],
  ])("announces the tab's first appearance with the From others wording (count=%i) (r3)", async (count, msg) => {
    setConv({ tabVisible: true, count, rows: [row()], forYou: [row()] });
    await mount();
    expect(container.querySelector(".conversations-announce")!.textContent).toBe(msg);
  });

  it("offers Show on the background only while hidden with rows (r1)", async () => {
    setConv({ status: "ready", count: 2, hidden: true, tabVisible: false });
    await mount();
    await act(async () => {
      container.querySelector(".pl-tabbar")!.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    });
    const bg = vi.mocked(openContextMenu).mock.calls.slice(-1)[0]![2] as {
      onShowConversations?: () => void;
      showConversationsLabel?: string;
    };
    expect(typeof bg.onShowConversations).toBe("function");
    expect(bg.showConversationsLabel).toBe("Show “From others”"); // §9 C7a desktop wording
    bg.onShowConversations!();
    expect(conversationsStore.show).toHaveBeenCalled();
  });

  it("right-clicking the tab offers only Hide (no Close/rename), which hides and returns to chat (r2)", async () => {
    setConv({ tabVisible: true, count: 1, rows: [row()], forYou: [row()] });
    await mount();
    await act(async () => pinnedTab()!.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true })));
    const ctx = vi.mocked(openContextMenu).mock.calls.slice(-1)[0]![2] as { onHideConversations?: () => void; onClose?: () => void; hideConversationsLabel?: string };
    expect(typeof ctx.onHideConversations).toBe("function");
    expect(ctx.hideConversationsLabel).toBe("Hide “From others”"); // §9 C7a desktop wording
    expect(ctx.onClose).toBeUndefined();
    ctx.onHideConversations!();
    expect(conversationsStore.hide).toHaveBeenCalled();
    expect(conversationsStore.openChat).toHaveBeenCalled();
  });

  it.each([
    [{ running: 1, needsYou: 0 }, ", 7 threads, 1 running, read-only", "info", "warning"],
    [{ running: 1, needsYou: 2 }, ", 7 threads, 1 running, 2 needs you, read-only", "warning", "info"],
  ])("names the count/status/read-only and colours the non-pulsing dot %o (r2)", async (counts, sr, on, off) => {
    setConv({ tabVisible: true, count: 7, ...counts, rows: [row()], forYou: [row()] });
    await mount();
    const pinned = pinnedTab()!;
    expect(pinned.querySelector(".conversations-tab__sr")!.textContent).toBe(sr);
    expect(pinned.querySelector(`.conversations-tab__icon .pl-dot--${on}`)).not.toBeNull();
    expect(pinned.querySelector(`.conversations-tab__icon .pl-dot--${off}`)).toBeNull();
    expect(pinned.querySelector(".pl-dot--pulse")).toBeNull();
  });

  it("selecting the tab switches stage; selecting a chat tab returns without changing currentSessionId (r3)", async () => {
    setConv({ tabVisible: true, stage: "chat", count: 1, rows: [row()], forYou: [row()] });
    await mount();
    await act(async () => pinnedTab()!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(conversationsStore.setStage).toHaveBeenCalledWith("conversations");
    expect(chatStore.switchSession).not.toHaveBeenCalled();
    const chatTab = Array.from(container.querySelectorAll(".pl-tabbar__tab"))[1];
    await act(async () => chatTab.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(conversationsStore.openChat).toHaveBeenCalled();
    expect(chatStore.switchSession).toHaveBeenCalledWith("chat-1");
    expect(chatStore.getSnapshot().currentSessionId).toBe("chat-1");
  });

  it("creating a chat while the Conversations tab is selected leaves the stage so the new chat shows (r3)", async () => {
    setConv({ tabVisible: true, stage: "conversations", count: 1, rows: [row()], forYou: [row()] });
    // The stage lives in the conversations store, which doesn't watch chat-store session changes —
    // so every "new chat" gesture must also leave the Conversations stage (openChat), or the new
    // chat is created but stays hidden behind the read-only list pane (the pool at display:none).
    const createSpy = vi.spyOn(chatStore, "createSession").mockImplementation(() => ({
      id: "new", title: "New chat", messages: [], createdAt: 2, updatedAt: 2,
    }));
    await mount();

    // The "+" add button.
    await act(async () => {
      container.querySelector(".pl-tabbar__add")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(conversationsStore.openChat).toHaveBeenCalledTimes(1);

    // The tab-bar background "New chat" menu entry does the same.
    createSpy.mockClear();
    vi.mocked(conversationsStore.openChat).mockClear();
    await act(async () => {
      container.querySelector(".pl-tabbar")!.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true }));
    });
    const bg = vi.mocked(openContextMenu).mock.calls.slice(-1)[0]![2] as { onNew?: () => void };
    bg.onNew!();
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(conversationsStore.openChat).toHaveBeenCalledTimes(1);
  });

  it("shows the read-only list pane (no composer) with For you, a collapsed Automated disclosure and Show older (r3)", async () => {
    setConv({
      tabVisible: true, stage: "conversations", count: 3, hasOlder: true, forYou: [row()],
      automated: [row({ session_id: "ctx-2", title: "Nightly sweep", origin_label: "From another agent", group: "automated" })],
    });
    await mount();
    const pane = container.querySelector(".conversations-pane")!;
    expect(pane.querySelector("h2")!.textContent).toBe("From others");
    expect(pane.querySelector(".pl-eyebrow")!.textContent).toBe("Read-only");
    expect(pane.textContent).toContain("other people and agents started with");
    expect(pane.textContent).toContain("type here");
    const disclosure = pane.querySelector(".conversations-disclosure")!;
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");
    expect(disclosure.getAttribute("aria-controls")).toBe("conversations-automated");
    expect(pane.querySelector("#conversations-automated")!.hasAttribute("hidden")).toBe(true);
    expect(pane.textContent).toContain("Show older");
    expect(pane.querySelector("textarea")).toBeNull(); // no composer
    expect((container.querySelector(".chat-session-pool") as HTMLElement).style.display).toBe("none");
  });

  it("renders a 4-row skeleton while a deep link loads (r4)", async () => {
    setConv({ status: "loading", tabVisible: true, stage: "conversations", deepLink: true });
    await mount();
    expect(container.querySelectorAll(".conversations-skeletons .pl-skel").length).toBe(4);
  });

  it("renders the failed state with a working Try again (r4)", async () => {
    setConv({ status: "error", tabVisible: true, stage: "conversations" });
    await mount();
    const pane = container.querySelector(".conversations-pane")!;
    expect(pane.querySelector(".pl-alert--error")).not.toBeNull();
    expect(pane.textContent).toContain("load threads from others");
    const retry = Array.from(pane.querySelectorAll("button")).find((b) => b.textContent === "Try again")!;
    await act(async () => retry.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(conversationsStore.retry).toHaveBeenCalled();
  });

  it("in the automated-only state, For you says nothing started and Automated opens by default (r4)", async () => {
    setConv({
      status: "ready", tabVisible: true, stage: "conversations", count: 2, forYou: [],
      automated: [row({ session_id: "ctx-9", group: "automated", origin_label: "From another agent" })],
    });
    await mount();
    const pane = container.querySelector(".conversations-pane")!;
    expect(pane.textContent).toContain("Nothing started for you this week.");
    expect(pane.querySelector(".conversations-disclosure")!.getAttribute("aria-expanded")).toBe("true");
    expect(pane.querySelector("#conversations-automated")!.hasAttribute("hidden")).toBe(false);
  });
});
