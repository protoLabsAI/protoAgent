// ChatResumeWatch wires two of the session-status reconciler's triggers into the app: a
// `chat.resumed` settle, and the tab becoming visible again (sessionLiveness.ts). Mounted for
// real against the real chatStore, with only the bus and the toast stubbed.

import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  handlers: new Map<string, (data: Record<string, unknown>) => void>(),
  replay: false,
  toast: vi.fn(),
  notify: vi.fn(),
}));

vi.mock("../lib/events", () => ({
  eventIsReplay: () => mocks.replay,
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    mocks.handlers.set(topic, fn);
    return () => mocks.handlers.delete(topic);
  },
}));
vi.mock("@protolabsai/ui/overlays", () => ({ useToast: () => mocks.toast }));
vi.mock("../lib/notify", () => ({ notifyIfHidden: mocks.notify }));

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

  it("a resume replayed to a console that was closed lands in the transcript without alerting", async () => {
    mocks.toast.mockClear();
    mocks.notify.mockClear();
    const live = seedUnmountedServerTurn("t-live");
    await act(async () =>
      mocks.handlers.get("chat.resumed")!({ session_id: live, task_id: "t-live", text: "Done just now.", state: "completed" }),
    );
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.notify).toHaveBeenCalledTimes(1);

    mocks.replay = true; // an event from before this console opened (lib/events `eventIsReplay`)
    const old = seedUnmountedServerTurn("t-old");
    await act(async () =>
      mocks.handlers.get("chat.resumed")!({ session_id: old, task_id: "t-old", text: "Finished hours ago.", state: "completed" }),
    );
    mocks.replay = false;
    const messages = chatStore.getSnapshot().sessions.find((s) => s.id === old)!.messages;
    expect(messages[messages.length - 1]).toMatchObject({ content: "Finished hours ago.", status: "done" });
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.notify).toHaveBeenCalledTimes(1);
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
