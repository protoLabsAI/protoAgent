// #2996 — ⌘K (chat.clear) now asks before wiping the conversation. The run() handler no
// longer deletes the session inline; it parks a clear request in the store, which the
// visible ChatSurface folds into a "Clear this conversation?" confirm dialog (harvest
// opt-in). These pin that the binding requests-through-the-dialog rather than destroying
// on the keypress — the confirm/harvest/cancel behavior itself lives with ChatSurface's
// dialog (covered in clearConfirmDialog.test.ts).
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import { chatStore } from "../chat/chat-store";
import { api } from "../lib/api";

beforeAll(async () => {
  await import("./coreKeybindings"); // registration side effect
});

afterEach(() => {
  vi.restoreAllMocks();
  // Drop every session so each test starts from a known store shape.
  for (const s of chatStore.getSnapshot().sessions) chatStore.deleteSession(s.id);
  chatStore.clearClearRequest();
});

function runClear() {
  const b = registeredKeybindings().find((k) => k.id === "chat.clear");
  expect(b, "binding chat.clear registered").toBeTruthy();
  b!.run(new KeyboardEvent("keydown", { key: "k" }));
}

describe("chat.clear — asks before wiping the conversation (#2996)", () => {
  it("run() requests a clear through the dialog, never deleting or wiping inline", () => {
    const session = chatStore.createSession();
    const request = vi.spyOn(chatStore, "requestClearSession");
    const del = vi.spyOn(api, "deleteChatSession").mockResolvedValue({ deleted: true, harvested: false });
    const wipe = vi.spyOn(chatStore, "updateMessages");

    runClear();

    // The binding parks the request for the current session…
    expect(request).toHaveBeenCalledWith(session.id);
    expect(chatStore.getSnapshot().pendingClearRequest).toBe(session.id);
    // …and does NOT perform the destructive work itself — that waits on the dialog's confirm.
    expect(del).not.toHaveBeenCalled();
    expect(wipe).not.toHaveBeenCalled();
  });

  it("run() is a safe no-op when there is no current session", () => {
    expect(chatStore.getSnapshot().currentSessionId).toBeNull();
    const request = vi.spyOn(chatStore, "requestClearSession");
    runClear();
    expect(request).not.toHaveBeenCalled();
    expect(chatStore.getSnapshot().pendingClearRequest).toBeNull();
  });
});
