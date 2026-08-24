// Reattach vs paused/rejected task states (#3082). The key invariant: a turn
// PAUSED on the operator (input-required / auth-required) must return the
// SESSION to idle — so the re-rendered HITL form's buttons work — WITHOUT
// finalize(), which would stamp the message "done" for a turn the server still
// owns. And the fallback poller must stop immediately on paused AND rejected
// states instead of spinning its full MAX_POLLS budget with the session stuck
// "streaming". Terminal turns (completed/failed/canceled) keep finalizing
// exactly as before (regression guard).
//
// Mocks only the api transport (importOriginal keeps the rest real, matching
// promptSlashCommand.test.ts); drives the REAL chatStore so session status and
// message status are asserted on actual store behavior.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { chatStore } from "./chat-store";
import { reattachTurn } from "./reattach";

vi.mock("../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api")>();
  return { ...mod, api: { ...mod.api, resumeTask: vi.fn(), getTask: vi.fn(), replayTask: vi.fn() } };
});

const resumeTask = vi.mocked(api.resumeTask);
const getTask = vi.mocked(api.getTask);
const replayTask = vi.mocked(api.replayTask);

// The mock resolutions are all synchronous promises, so one macrotask drains
// the whole run()/fallbackPoll() chain; a second is cheap insurance.
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

const ASSISTANT_ID = "a1";
const TASK_ID = "t1";

/** A session whose last assistant message is stuck `streaming` — the exact
 *  shape the ChatSurface reattach effect hands to reattachTurn. */
function seedStuckSession(): string {
  const session = chatStore.createSession();
  chatStore.updateMessages(session.id, [
    { id: "u1", role: "user", content: "deploy the release", status: "done" },
    { id: ASSISTANT_ID, role: "assistant", content: "partial answer", status: "streaming", taskId: TASK_ID },
  ]);
  chatStore.setSessionStatus(session.id, "streaming");
  return session.id;
}

function assistantMessage(sessionId: string) {
  return chatStore
    .getSnapshot()
    .sessions.find((s) => s.id === sessionId)
    ?.messages.find((m) => m.id === ASSISTANT_ID);
}

function sessionStatus(sessionId: string) {
  return chatStore.getSnapshot().sessionStatusMap[sessionId];
}

let cancels: Array<() => void> = [];
function attach(sessionId: string, hooks?: Parameters<typeof reattachTurn>[3]) {
  const cancel = reattachTurn(sessionId, ASSISTANT_ID, TASK_ID, hooks);
  cancels.push(cancel);
  return cancel;
}

beforeEach(() => {
  resumeTask.mockReset();
  getTask.mockReset();
  replayTask.mockReset();
});

afterEach(() => {
  cancels.forEach((cancel) => cancel());
  cancels = [];
});

// ---------------------------------------------------------------------------
// run(): the resubscribe stream closed, GetTask reports a PAUSED state
// ---------------------------------------------------------------------------

describe("reattach run(): paused states", () => {
  it.each(["TASK_STATE_INPUT_REQUIRED", "input-required", "TASK_STATE_AUTH_REQUIRED", "auth-required"])(
    "goes idle WITHOUT finalizing the message on %s",
    async (state) => {
      const sessionId = seedStuckSession();
      resumeTask.mockResolvedValue(undefined);
      getTask.mockResolvedValue({ state, text: "" });

      attach(sessionId);
      await settle();

      // Session un-busied → HitlForm's busy={status === "streaming"} is false.
      expect(sessionStatus(sessionId)).toBe("idle");
      // finalize() was NOT called: message status and content preserved as-is.
      const msg = assistantMessage(sessionId);
      expect(msg?.status).toBe("streaming");
      expect(msg?.content).toBe("partial answer");
      // Never dropped into the poller for a settled outcome.
      expect(replayTask).not.toHaveBeenCalled();
    },
  );
});

// ---------------------------------------------------------------------------
// fallbackPoll(): resubscribe rejected, the poller consults the durable task
// ---------------------------------------------------------------------------

describe("reattach fallbackPoll(): paused and rejected states", () => {
  it("stops polling immediately and goes idle without finalize on input-required", async () => {
    const sessionId = seedStuckSession();
    // Non-cold rejection (the real server's UnsupportedOperationError for a
    // non-running task) → run() breaks straight into fallbackPoll().
    resumeTask.mockRejectedValue(new Error("task is not running (UnsupportedOperationError)"));
    replayTask.mockResolvedValue("TASK_STATE_INPUT_REQUIRED");

    attach(sessionId);
    await settle();

    expect(sessionStatus(sessionId)).toBe("idle");
    expect(assistantMessage(sessionId)?.status).toBe("streaming");
    // ONE poll, not the 10-minute MAX_POLLS loop.
    expect(replayTask).toHaveBeenCalledTimes(1);
    // The paused branch returns before the finalize branch's GetTask.
    expect(getTask).not.toHaveBeenCalled();
  });

  it("re-renders the pending HITL form through the snapshot replay hooks", async () => {
    const sessionId = seedStuckSession();
    resumeTask.mockRejectedValue(new Error("task is not running (UnsupportedOperationError)"));
    replayTask.mockImplementation(async (_taskId, _sessionId, handlers) => {
      handlers?.onInputRequired?.({ kind: "approval", title: "Approve the deploy?" });
      return "TASK_STATE_INPUT_REQUIRED";
    });
    const onHitl = vi.fn();

    attach(sessionId, { onHitl });
    await settle();

    expect(onHitl).toHaveBeenCalledWith(expect.objectContaining({ title: "Approve the deploy?" }));
    expect(sessionStatus(sessionId)).toBe("idle");
  });

  it("treats rejected as TERMINAL: finalizes on the first poll instead of looping", async () => {
    const sessionId = seedStuckSession();
    resumeTask.mockRejectedValue(new Error("task is not running (UnsupportedOperationError)"));
    replayTask.mockResolvedValue("TASK_STATE_REJECTED");
    getTask.mockResolvedValue({ state: "TASK_STATE_REJECTED", text: "" });

    attach(sessionId);
    await settle();

    expect(replayTask).toHaveBeenCalledTimes(1);
    expect(sessionStatus(sessionId)).toBe("idle");
    // Rejected is settled — the bubble must not stay `streaming`.
    expect(assistantMessage(sessionId)?.status).toBe("done");
  });
});

// ---------------------------------------------------------------------------
// Regression guard: terminal turns finalize exactly as before
// ---------------------------------------------------------------------------

describe("reattach: terminal turns are unchanged", () => {
  it("completed → finalize lands the answer, message done, session idle", async () => {
    const sessionId = seedStuckSession();
    resumeTask.mockResolvedValue(undefined);
    getTask.mockResolvedValue({ state: "TASK_STATE_COMPLETED", text: "the full answer" });

    attach(sessionId);
    await settle();

    const msg = assistantMessage(sessionId);
    expect(msg?.status).toBe("done");
    expect(msg?.content).toBe("the full answer");
    expect(sessionStatus(sessionId)).toBe("idle");
  });

  it("failed → message error, session error", async () => {
    const sessionId = seedStuckSession();
    resumeTask.mockResolvedValue(undefined);
    getTask.mockResolvedValue({ state: "TASK_STATE_FAILED", text: "boom" });

    attach(sessionId);
    await settle();

    expect(assistantMessage(sessionId)?.status).toBe("error");
    expect(sessionStatus(sessionId)).toBe("error");
  });

  it("canceled (via the poller) → message error, session error", async () => {
    const sessionId = seedStuckSession();
    resumeTask.mockRejectedValue(new Error("task is not running (UnsupportedOperationError)"));
    replayTask.mockResolvedValue("TASK_STATE_CANCELLED");
    getTask.mockResolvedValue({ state: "TASK_STATE_CANCELLED", text: "" });

    attach(sessionId);
    await settle();

    expect(replayTask).toHaveBeenCalledTimes(1);
    expect(assistantMessage(sessionId)?.status).toBe("error");
    expect(sessionStatus(sessionId)).toBe("error");
  });
});
