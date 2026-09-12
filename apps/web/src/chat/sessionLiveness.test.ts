// The session-status reconciler (sessionLiveness.ts). Each guard gets a test that isolates
// it: the state it guards is built so that ONLY that guard stands between a live turn and a
// wrongful idle. Drives the real chatStore.

import { afterEach, describe, expect, it, vi } from "vitest";

import { applyProgressFrame } from "../app/serverTurnProgress";
import type { ChatMessage } from "../lib/types";
import { chatStore } from "./chat-store";
import { liveMessageId } from "./server-turn-store";
import {
  beginLocalTurn,
  beginReattach,
  reconcileAllSessionStatuses,
  reconcileSessionStatus,
  watchSessionLiveness,
} from "./sessionLiveness";

let ends: Array<() => void> = [];
afterEach(() => {
  ends.forEach((end) => end());
  ends = [];
  vi.restoreAllMocks();
});

function claimLocal(sessionId: string) {
  const end = beginLocalTurn(sessionId);
  ends.push(end);
  return end;
}

function claimReattach(sessionId: string) {
  const end = beginReattach(sessionId);
  ends.push(end);
  return end;
}

/** A session reading "streaming" with this transcript. */
function seed(messages: ChatMessage[], status: "streaming" | "error" = "streaming"): string {
  const session = chatStore.createSession();
  chatStore.updateMessages(session.id, messages);
  chatStore.setSessionStatus(session.id, status);
  return session.id;
}

const status = (sessionId: string) => chatStore.getSnapshot().sessionStatusMap[sessionId];

const ENDED: ChatMessage[] = [
  { id: "u1", role: "user", content: "summarize the PR", status: "done" },
  { id: "a1", role: "assistant", content: "Summarized.", status: "done", taskId: "t1" },
];

describe("reconcileSessionStatus", () => {
  it("hands back a session that reads streaming with nothing live", () => {
    const sessionId = seed(ENDED);
    expect(reconcileSessionStatus(sessionId)).toBe(true);
    expect(status(sessionId)).toBe("idle");
  });

  it("only ever moves streaming to idle: an errored session keeps its error", () => {
    const sessionId = seed(ENDED, "error");
    expect(reconcileSessionStatus(sessionId)).toBe(false);
    expect(status(sessionId)).toBe("error");
  });

  it("a local turn in its post-stream window keeps the session", () => {
    // runTurn's onDone settles the bubble, then the stream awaits a GetTask reconcile before
    // it goes idle. Idle here would bring Send back mid-turn and invite a second stream.
    const sessionId = seed(ENDED);
    const end = claimLocal(sessionId);
    expect(reconcileSessionStatus(sessionId)).toBe(false);
    expect(status(sessionId)).toBe("streaming");
    end();
    expect(reconcileSessionStatus(sessionId)).toBe(true);
  });

  it("a live reattach keeps the session", () => {
    const sessionId = seed(ENDED);
    const end = claimReattach(sessionId);
    expect(reconcileSessionStatus(sessionId)).toBe(false);
    expect(status(sessionId)).toBe("streaming");
    end();
    expect(reconcileSessionStatus(sessionId)).toBe(true);
  });

  it("a delegate's settled row after the lead's still-streaming preview keeps the session", () => {
    // The #3449 room shape: a participant's row lands, already settled, AFTER the preview
    // while the lead's turn runs. The LAST assistant row is settled; the turn is not.
    const liveId = liveMessageId("t1", "s-room");
    const preview: ChatMessage = { id: liveId, role: "assistant", content: "Asking claude-code…", status: "streaming", taskId: "t1" };
    const session = chatStore.createSession();
    const messages = applyProgressFrame(
      [{ id: "u1", role: "user", content: "ask claude-code to check the diff", status: "done" }, preview],
      { session: session.id, taskId: "t1", kind: "room", id: "r1", author: "claude-code", text: "The diff is clean.", ok: true },
    );
    chatStore.updateMessages(session.id, messages);
    chatStore.setSessionStatus(session.id, "streaming");
    expect(messages[messages.length - 1]).toMatchObject({ author: { name: "claude-code" }, status: "done" });

    expect(reconcileSessionStatus(session.id)).toBe(false);
    expect(status(session.id)).toBe("streaming");
  });

  it("counts claims: one of two overlapping reattaches ending leaves the session held", () => {
    const sessionId = seed(ENDED);
    const first = claimReattach(sessionId);
    claimReattach(sessionId);
    first();
    first(); // idempotent: a cancel and the run's own unwind both end the same claim
    expect(reconcileSessionStatus(sessionId)).toBe(false);
  });

  it("going idle is a status write and nothing else", () => {
    // It must not reach for the interjection ladder: an immediate re-check would spend the
    // grace an unaccounted-for interjection gets, and a transcript write could hand one back.
    const sessionId = seed(ENDED);
    const updateMessages = vi.spyOn(chatStore, "updateMessages");
    const setSessionStatus = vi.spyOn(chatStore, "setSessionStatus");
    reconcileSessionStatus(sessionId);
    expect(setSessionStatus.mock.calls).toEqual([[sessionId, "idle"]]);
    expect(updateMessages).not.toHaveBeenCalled();
  });
});

describe("the visibility trigger", () => {
  it("reconciles every session when the tab becomes visible, and leaves live ones alone", () => {
    const stuck = seed(ENDED);
    const live = seed([
      { id: "u1", role: "user", content: "run the tests", status: "done" },
      { id: "a1", role: "assistant", content: "", status: "streaming", taskId: "t2" },
    ]);
    const unwatch = watchSessionLiveness();
    try {
      document.dispatchEvent(new Event("visibilitychange"));
      expect(status(stuck)).toBe("idle");
      expect(status(live)).toBe("streaming");
    } finally {
      unwatch();
    }
  });

  it("does nothing while the tab is hidden", () => {
    const stuck = seed(ENDED);
    const unwatch = watchSessionLiveness();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    try {
      document.dispatchEvent(new Event("visibilitychange"));
      expect(status(stuck)).toBe("streaming");
    } finally {
      visibility.mockRestore();
      unwatch();
    }
  });

  it("reconcileAllSessionStatuses touches only sessions reading streaming", () => {
    const stuck = seed(ENDED);
    const errored = seed(ENDED, "error");
    reconcileAllSessionStatuses();
    expect(status(stuck)).toBe("idle");
    expect(status(errored)).toBe("error");
  });
});
