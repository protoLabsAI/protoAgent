// #2968 — Escape stops the streaming turn / cancels the newest queued steer (the
// Claude.ai/ChatGPT convention). These pin the `chat.stop` registration (default Escape,
// chat-scoped, live while typing in the composer — so it appears in Settings ▸ Keyboard
// under Chat and is rebindable) and that its `run` goes through the escapeStop seam the
// visible ChatSurface slot publishes its behavior on. The slash-menu / HITL Escape paths
// preventDefault in their own React handlers and the keydown host skips defaultPrevented
// events, so they win without ever reaching this binding.
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import { registerChatEscapeHandler } from "../chat/escapeStop";
import { useKeybindingOverrides } from "./overrides";
import { resolveBinding } from "./resolve";

beforeAll(async () => {
  await import("./coreKeybindings"); // registration side effect
});

afterEach(() => {
  useKeybindingOverrides.getState().resetAll();
});

const resolve = (combo: string, scopes: string[] = [], editable = false) =>
  resolveBinding(registeredKeybindings(), combo, { scopes: new Set(scopes), editable });

describe("chat.stop — Escape stops streaming / cancels queued steer (#2968)", () => {
  it("registers on escape — chat-scoped, grouped under Chat, live while typing", () => {
    const b = registeredKeybindings().find((k) => k.id === "chat.stop");
    expect(b?.defaultKeys).toBe("escape");
    expect(b?.scope).toBe("chat");
    expect(b?.allowInInput).toBe(true);
    expect(b?.group).toBe("Chat"); // Settings ▸ Keyboard lists it in the Chat group
    expect(b?.label).toBeTruthy();
  });

  it("escape inside the chat surface resolves to chat.stop, even mid-typing", () => {
    expect(resolve("escape", ["chat"])?.id).toBe("chat.stop");
    expect(resolve("escape", ["chat"], true)?.id).toBe("chat.stop");
  });

  it("escape outside the chat surface resolves to nothing", () => {
    expect(resolve("escape")).toBeNull();
    expect(resolve("escape", ["settings"])).toBeNull();
  });

  it("run() invokes the handler the visible chat slot registered on the seam", () => {
    const handler = vi.fn();
    const off = registerChatEscapeHandler(handler);
    const b = registeredKeybindings().find((k) => k.id === "chat.stop");
    expect(b, "binding chat.stop registered").toBeTruthy();
    b!.run(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(handler).toHaveBeenCalledTimes(1);
    off();
  });

  it("run() is a safe no-op when no chat slot is mounted", () => {
    const b = registeredKeybindings().find((k) => k.id === "chat.stop");
    expect(() => b!.run(new KeyboardEvent("keydown", { key: "Escape" }))).not.toThrow();
  });

  it("a Settings ▸ Keyboard override rebinds it — escape then goes dead in chat", () => {
    useKeybindingOverrides.getState().setBinding("chat.stop", "mod+.");
    expect(resolve("mod+.", ["chat"])?.id).toBe("chat.stop");
    expect(resolve("escape", ["chat"])).toBeNull();
  });
});
