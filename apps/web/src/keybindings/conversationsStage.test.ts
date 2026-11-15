// @vitest-environment jsdom
// Other conversations C2 (docs/design/other-conversations.md §3.3/§4.7): the chat switch/create
// bindings must also leave the read-only Conversations stage. The stage lives in the SEPARATE
// conversations store, which doesn't watch chat-store session changes, so without this a new or
// switched chat is created but stays hidden behind the Conversations list pane (the chat pool sits
// at display:none while that stage is selected) — the shortcut would appear to do nothing.
import { beforeAll, beforeEach, describe, expect, it } from "vitest";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import { chatStore } from "../chat/chat-store";
import {
  setStage as setConversationsStage,
  snapshotForTest as conversationsSnapshot,
  __resetForTest as resetConversations,
} from "../chat/conversations-store";

beforeAll(async () => {
  await import("./coreKeybindings"); // registration side effect
});

function runBinding(id: string) {
  const b = registeredKeybindings().find((k) => k.id === id);
  expect(b, `binding ${id} registered`).toBeTruthy();
  b!.run(new KeyboardEvent("keydown"));
}

describe("chat bindings leave the Conversations stage (C2 §3.3/§4.7)", () => {
  beforeEach(() => {
    resetConversations();
    setConversationsStage("conversations");
    expect(conversationsSnapshot().stage).toBe("conversations");
  });

  it("New chat (chat.new / mod+t) resets the stage to chat so the new chat shows", () => {
    runBinding("chat.new");
    expect(conversationsSnapshot().stage).toBe("chat");
  });

  it("switching to the next chat tab (chat.tab.next) resets the stage to chat", () => {
    chatStore.createSession(); // a session must exist for the offset switch to act
    setConversationsStage("conversations"); // createSession doesn't touch the stage
    runBinding("chat.tab.next");
    expect(conversationsSnapshot().stage).toBe("chat");
  });
});
