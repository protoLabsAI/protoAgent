// ChatResumeWatch wires two of the session-status reconciler's triggers into the app: a
// `chat.resumed` settle, and the tab becoming visible again (sessionLiveness.ts). Mounted for
// real against the real chatStore, with only the bus and the toast stubbed.

import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  handlers: new Map<string, (data: Record<string, unknown>) => void>(),
}));

vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    mocks.handlers.set(topic, fn);
    return () => mocks.handlers.delete(topic);
  },
}));
vi.mock("@protolabsai/ui/overlays", () => ({ useToast: () => () => {} }));
vi.mock("../lib/notify", () => ({ notifyIfHidden: () => {} }));

import { chatStore } from "../chat/chat-store";
import { liveMessageId } from "../chat/server-turn-store";
import { ChatResumeWatch } from "./ChatResumeWatch";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;

beforeEach(async () => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(h(ChatResumeWatch)));
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

const status = (sessionId: string) => chatStore.getSnapshot().sessionStatusMap[sessionId];

/** A session with no slot to reattach it (it is not mounted here), reading "streaming"
 *  because boot saw its server-fired turn live: the preview is still streaming. */
function seedUnmountedServerTurn(taskId: string): string {
  const session = chatStore.createSession();
  chatStore.updateMessages(session.id, [
    { id: "u1", role: "user", content: "run the nightly report", status: "done" },
    { id: liveMessageId(taskId, session.id), role: "assistant", content: "Reading…", status: "streaming", taskId },
  ]);
  chatStore.setSessionStatus(session.id, "streaming");
  return session.id;
}

describe("ChatResumeWatch and the session-status reconciler", () => {
  it("a turn's `chat.resumed` hands back a session no slot will", async () => {
    const sessionId = seedUnmountedServerTurn("t-report");
    await act(async () =>
      mocks.handlers.get("chat.resumed")!({ session_id: sessionId, task_id: "t-report", text: "Report is clean.", state: "completed" }),
    );
    const messages = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)!.messages;
    expect(messages[messages.length - 1]).toMatchObject({
      content: "Report is clean.",
      status: "done",
    });
    expect(status(sessionId)).toBe("idle");
  });

  it("another task's `chat.resumed` leaves a session whose own turn is still live", async () => {
    const sessionId = seedUnmountedServerTurn("t-report");
    await act(async () =>
      mocks.handlers.get("chat.resumed")!({ session_id: sessionId, task_id: "t-backup", text: "Backup done.", state: "completed" }),
    );
    expect(status(sessionId)).toBe("streaming");
  });

  it("the tab becoming visible reconciles a session left streaming with nothing live", async () => {
    const session = chatStore.createSession();
    chatStore.updateMessages(session.id, [
      { id: "u1", role: "user", content: "summarize", status: "done" },
      { id: "a1", role: "assistant", content: "Done.", status: "done", taskId: "t1" },
    ]);
    chatStore.setSessionStatus(session.id, "streaming");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(status(session.id)).toBe("idle");
  });
});
