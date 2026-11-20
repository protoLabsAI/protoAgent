// @vitest-environment jsdom
// H-U1: the tab strip pins Home FIRST, but the chat store appends the Home session. The tab
// keybindings must walk the strip's visible order, so ⌘1 is the tab drawn first (Home) and
// Ctrl+Tab cycles in the order the tabs are shown — not in raw store order.
import { beforeAll, beforeEach, describe, expect, it } from "vitest";

import { chatStore } from "../chat/chat-store";
import { homeQueryKey, homeSessionId, inTabOrder } from "../chat/homeTab";
import { registeredKeybindings } from "../ext/keybindingRegistry";
import { queryClient } from "../lib/queryClient";

beforeAll(async () => {
  await import("./coreKeybindings"); // registration side effect
});

function runBinding(id: string) {
  const b = registeredKeybindings().find((k) => k.id === id);
  expect(b, `binding ${id} registered`).toBeTruthy();
  b!.run(new KeyboardEvent("keydown"));
}
const current = () => chatStore.getSnapshot().currentSessionId;

describe("tab keybindings follow the strip order with a pinned Home tab", () => {
  let first: string;
  let second: string;

  beforeEach(() => {
    queryClient.clear();
    chatStore.ensureSession("kb-a", "A");
    chatStore.ensureSession("kb-b", "B");
    chatStore.ensureSession("home", "Home"); // appended LAST, as ChatSurface does
    const ordinary = chatStore.getSnapshot().sessions.filter((s) => s.id !== "home");
    [first, second] = [ordinary[0].id, ordinary[1].id];
    expect(chatStore.getSnapshot().sessions[0].id).toBe(first);
  });

  it("without Home state (flag off / 404) the bindings keep raw store order", () => {
    queryClient.setQueryData(homeQueryKey(), null);
    expect(homeSessionId()).toBeUndefined();
    runBinding("chat.tab.1");
    expect(current()).toBe(first);
    runBinding("chat.tab.2");
    expect(current()).toBe(second);
  });

  it("mod+1 jumps to Home and mod+2 to the first ordinary chat", () => {
    queryClient.setQueryData(homeQueryKey(), { session_id: "home", last_read_at: null });
    runBinding("chat.tab.2");
    expect(current()).toBe(first);
    runBinding("chat.tab.1");
    expect(current()).toBe("home");
    runBinding("chat.tab.3");
    expect(current()).toBe(second);
  });

  it("next/prev cycle in strip order: Home → first chat, and back wraps to the last tab", () => {
    queryClient.setQueryData(homeQueryKey(), { session_id: "home", last_read_at: null });
    const order = inTabOrder(chatStore.getSnapshot().sessions).map((s) => s.id);
    expect(order[0]).toBe("home");
    chatStore.switchSession("home");
    runBinding("chat.tab.next");
    expect(current()).toBe(first);
    runBinding("chat.tab.prev");
    expect(current()).toBe("home");
    runBinding("chat.tab.prev.bracket");
    expect(current()).toBe(order[order.length - 1]);
    runBinding("chat.tab.next.bracket");
    expect(current()).toBe("home");
  });

  it("a Home id that is not an open session leaves the order untouched", () => {
    queryClient.setQueryData(homeQueryKey(), { session_id: "not-open", last_read_at: null });
    expect(inTabOrder(chatStore.getSnapshot().sessions).map((s) => s.id))
      .toEqual(chatStore.getSnapshot().sessions.map((s) => s.id));
  });
});
