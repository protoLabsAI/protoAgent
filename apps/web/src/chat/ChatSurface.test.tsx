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
