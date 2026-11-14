// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Heavy UI/nav deps are stubbed so the test exercises only the store/sync wiring.
vi.mock("../app/palette/nav", () => ({ openView: vi.fn() }));
vi.mock("../keybindings/intents", () => ({ useKbIntents: { getState: () => ({ focusComposer: vi.fn() }) } }));
vi.mock("../state/uiStore", () => ({ useUI: { getState: () => ({ closeSurfaceModal: vi.fn() }) } }));
vi.mock("../chat/sessionListSync", () => ({ pushReopen: vi.fn() }));
vi.mock("../chat/sessionHydration", () => ({
  SESSION_TURN_LIMIT: 50,
  // A minimal buildable session for the id asked for — enough to hydrate into the store.
  sessionFromDurableTurns: vi.fn((summary: { session_id: string }) => ({
    id: summary.session_id,
    title: "Source chat",
    messages: [{ id: "a", role: "assistant", content: "answer", status: "done", taskId: "t1" }],
    createdAt: 1,
    updatedAt: 1,
  })),
}));

import { api } from "../lib/api";
import { chatStore } from "../chat/chat-store";
import { pushReopen } from "../chat/sessionListSync";
import { openSourceChat } from "./openSourceChat";

beforeEach(() => {
  vi.clearAllMocks(); // forget the module-mock call histories (pushReopen) between cases
  vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [{ task_id: "t1" }] as never, live_task_id: null });
});
afterEach(() => vi.restoreAllMocks());

describe("openSourceChat (ADR 0130 S6)", () => {
  it("pushes a server reopen when the source chat was CLOSED (dismissed here)", async () => {
    const id = "chat-closed-elsewhere";
    chatStore.dismissSession(id); // mirrors a close that arrived via the server list
    await openSourceChat(id);
    // Without the server reopen, the next resync (server still has it closed) would drop the tab
    // again — so opening a decision's source chat must tell the server to reopen it. (openSourceChat
    // clears the local dismissal itself.)
    expect(pushReopen).toHaveBeenCalledWith(id);
  });

  it("does NOT reopen a chat that was merely never seen locally (background/scheduled source)", async () => {
    const id = "chat-never-seen";
    await openSourceChat(id);
    expect(pushReopen).not.toHaveBeenCalled();
  });
});
