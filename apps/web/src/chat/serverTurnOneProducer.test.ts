// One producer per bubble for a server-fired turn.
//
// A background push-resume (ADR 0070), scheduled fire or watch reaction runs by
// self-POSTing into a session, so the browser never streams it. The bus republishes its
// frames as `chat.progress` and ServerTurnWatch folds them into a live preview bubble
// (#2361). That preview is `streaming` with a `taskId` — exactly the shape ChatSurface's
// reattach effect looks for — and since #3178 the effect re-runs on `reattachKey`, so the
// preview's FIRST frame triggered a resubscribe to the SAME still-running task. From then
// on the resubscribe stream and the bus both wrote every chunk into one bubble: the
// operator watched duplicated, interleaved text until the final answer replaced the
// bubble wholesale and made it look fine again.
//
// These drive the REAL chatStore, the REAL reattachTurn and the REAL bus fold; only the
// A2A transport is mocked. The duplication only ever existed MID-stream — the post-stream
// finalize replaces the bubble with canonical text — so that is where they assert.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { foldProgressEvent } from "../app/ServerTurnWatch";
import { api } from "../lib/api";
import { chatStore } from "./chat-store";
import { isReattaching, reattachTurn, shouldReattach } from "./reattach";
import { liveMessageId, noteTurnFinished, noteTurnStarted, resetServerTurns } from "./server-turn-store";

vi.mock("../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api")>();
  return { ...mod, api: { ...mod.api, resumeTask: vi.fn(), getTask: vi.fn(), replayTask: vi.fn() } };
});

const resumeTask = vi.mocked(api.resumeTask);
const getTask = vi.mocked(api.getTask);

const TASK = "bg-task-1";

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function bubble(sessionId: string) {
  return chatStore
    .getSnapshot()
    .sessions.find((s) => s.id === sessionId)
    ?.messages.find((m) => m.id === liveMessageId(TASK, sessionId));
}

/** What the operator actually sees: ChatMessageView renders from parts when it has any. */
function rendered(sessionId: string): string {
  const m = bubble(sessionId);
  if (!m) return "";
  const parts = m.parts ?? [];
  return parts.length ? parts.map((p) => (p.kind === "text" ? p.text : "")).join("") : m.content;
}

function progressText(sessionId: string, text: string) {
  foldProgressEvent({ session_id: sessionId, task_id: TASK, phase: "text", text });
}

let cancels: Array<() => void> = [];

beforeEach(() => {
  resumeTask.mockReset();
  getTask.mockReset();
  resetServerTurns();
});

afterEach(() => {
  cancels.forEach((cancel) => cancel());
  cancels = [];
  resetServerTurns();
});

describe("a server-fired turn's preview has one producer", () => {
  it("does not also land the bus's copy of a chunk while a reattach is streaming it", async () => {
    // The shape a reload / mid-turn open produces: the console never saw the turn start,
    // so a reattach legitimately drives the preview — and the bus keeps publishing.
    const session = chatStore.createSession();
    progressText(session.id, "Hello "); // the bus creates the preview bubble

    let releaseStream!: () => void;
    const streamHeldOpen = new Promise<void>((resolve) => (releaseStream = resolve));
    resumeTask.mockImplementation(async (_task, _session, handlers) => {
      handlers?.onTaskSnapshot?.(); // replay what was missed…
      handlers?.onText?.("Hello ", false);
      handlers?.onText?.("world", true); // …then the live tail
      await streamHeldOpen; // the turn is still running
    });
    getTask.mockResolvedValue({ state: "completed", text: "Hello world" });

    cancels.push(reattachTurn(session.id, liveMessageId(TASK, session.id), TASK));
    await settle();
    progressText(session.id, "world"); // the bus republishes the SAME chunk

    // Mid-stream — before the finalize's canonical replace could paper over it.
    expect(rendered(session.id)).toBe("Hello world");

    releaseStream();
    await settle();
    expect(rendered(session.id)).toBe("Hello world");
  });

  it("does not reattach a preview whose turn this console is watching live", () => {
    // The common case: turn.started arrived, the bus is feeding the preview, and
    // chat.resumed will settle it. A reattach here is the second producer.
    const session = chatStore.createSession();
    noteTurnStarted(session.id, "Background task finished");
    progressText(session.id, "Working on it");

    expect(shouldReattach(bubble(session.id), session.id)).toBe(false);
  });

  it("still reattaches a preview whose turn this console never saw running", () => {
    // A reload or a mid-turn open: nothing told this console the turn is live, so the
    // reattach is the only way to catch up — the self-heal the preview's taskId exists for.
    const session = chatStore.createSession();
    progressText(session.id, "Working on it");

    expect(shouldReattach(bubble(session.id), session.id)).toBe(true);
  });

  it("reattaches a preview left streaming after its turn finished", () => {
    const session = chatStore.createSession();
    noteTurnStarted(session.id, "Background task finished");
    progressText(session.id, "Working on it");
    noteTurnFinished(session.id); // …but chat.resumed never settled it

    expect(shouldReattach(bubble(session.id), session.id)).toBe(true);
  });

  it("keeps reattaching an ordinary streaming turn while a server turn runs in the same session", () => {
    // #3178's own purpose must survive: only the server-turn PREVIEW defers to the bus.
    const session = chatStore.createSession();
    noteTurnStarted(session.id, "Background task finished");
    const stuck = { id: "a1", role: "assistant" as const, content: "partial", status: "streaming" as const, taskId: "t9" };

    expect(shouldReattach(stuck, session.id)).toBe(true);
  });

  it("still delivers a room reply while a reattach drives the turn's preview", async () => {
    const session = chatStore.createSession();
    resumeTask.mockImplementation(() => new Promise(() => {}));
    cancels.push(reattachTurn(session.id, liveMessageId(TASK, session.id), TASK));
    foldProgressEvent({
      session_id: session.id,
      task_id: TASK,
      phase: "room_reply",
      message_id: "r1",
      author: "hermes",
      text: "done on my side",
      ok: true,
    });
    const messages = chatStore.getSnapshot().sessions.find((s) => s.id === session.id)?.messages ?? [];
    expect(messages.some((m) => m.id === "background-room-r1")).toBe(true);
  });
});

describe("the reattach registry", () => {
  it("tracks a reattach for exactly as long as it runs", async () => {
    const session = chatStore.createSession();
    resumeTask.mockImplementation(() => new Promise(() => {}));
    const cancel = reattachTurn(session.id, "m1", TASK);
    expect(isReattaching("m1")).toBe(true);
    cancel();
    expect(isReattaching("m1")).toBe(false);
  });

  it("does not let an earlier reattach release a newer one of the same message", () => {
    const session = chatStore.createSession();
    resumeTask.mockImplementation(() => new Promise(() => {}));
    const first = reattachTurn(session.id, "m1", TASK);
    const second = reattachTurn(session.id, "m1", TASK);
    first();
    expect(isReattaching("m1")).toBe(true);
    second();
    expect(isReattaching("m1")).toBe(false);
  });

  it("releases when the stream ends on its own", async () => {
    const session = chatStore.createSession();
    resumeTask.mockResolvedValue(undefined);
    getTask.mockResolvedValue({ state: "completed", text: "done" });
    cancels.push(reattachTurn(session.id, "m1", TASK));
    await settle();
    expect(isReattaching("m1")).toBe(false);
  });
});
