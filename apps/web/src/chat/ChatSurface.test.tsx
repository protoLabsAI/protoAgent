import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import { canSubmitChatDraft, chatComposerBusy, resolveComposerStopTarget } from "./ChatSurface";

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
