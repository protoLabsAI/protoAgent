// #2949 — ⌘K now clears the chat (the Claude.ai/ChatGPT convention) and the command
// palette moved to ⌘⇧K. These pin the swapped DEFAULTS and how they resolve: the
// chat-scoped clear wins ⌘K inside the chat surface, ⌘K is a no-op elsewhere (the
// palette no longer claims it), and a Settings ▸ Keyboard override still beats both.
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { chatStore } from "../chat/chat-store";
import { registeredKeybindings } from "../ext/keybindingRegistry";
import { useKeybindingOverrides } from "./overrides";
import { resolveBinding } from "./resolve";

beforeAll(async () => {
  await import("./coreKeybindings"); // registration side effect
});

afterEach(() => {
  useKeybindingOverrides.getState().resetAll();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const resolve = (combo: string, scopes: string[] = [], editable = false) =>
  resolveBinding(registeredKeybindings(), combo, { scopes: new Set(scopes), editable });

describe("swapped ⌘K / ⌘⇧K defaults (#2949)", () => {
  it("registers chat.clear on mod+k — chat-scoped, live while typing", () => {
    const b = registeredKeybindings().find((k) => k.id === "chat.clear");
    expect(b?.defaultKeys).toBe("mod+k");
    expect(b?.scope).toBe("chat");
    expect(b?.allowInInput).toBe(true);
  });

  it("registers palette.toggle on mod+shift+k — global", () => {
    const b = registeredKeybindings().find((k) => k.id === "palette.toggle");
    expect(b?.defaultKeys).toBe("mod+shift+k");
    expect(b?.scope).toBeUndefined();
  });

  it("mod+k inside the chat surface resolves to chat.clear, even mid-typing", () => {
    expect(resolve("mod+k", ["chat"])?.id).toBe("chat.clear");
    expect(resolve("mod+k", ["chat"], true)?.id).toBe("chat.clear");
  });

  it("mod+k outside the chat surface resolves to nothing", () => {
    expect(resolve("mod+k")).toBeNull();
    expect(resolve("mod+k", ["settings"])).toBeNull();
  });

  it("mod+shift+k opens the palette from anywhere, including inside chat", () => {
    expect(resolve("mod+shift+k")?.id).toBe("palette.toggle");
    expect(resolve("mod+shift+k", ["chat"])?.id).toBe("palette.toggle");
    expect(resolve("mod+shift+k", [], true)?.id).toBe("palette.toggle");
  });

  it("a Settings ▸ Keyboard override still beats the new defaults", () => {
    useKeybindingOverrides.getState().setBinding("chat.clear", "mod+shift+x");
    expect(resolve("mod+shift+x", ["chat"])?.id).toBe("chat.clear");
    // …and mod+k goes dead in the chat surface once clear is rebound off it.
    expect(resolve("mod+k", ["chat"])).toBeNull();
  });
});

describe("chat tab bracket defaults", () => {
  it.each([
    ["prev", "[", "Previous", "ctrl+shift+tab", 0, 2],
    ["next", "]", "Next", "ctrl+tab", 2, 0],
  ] as const)("registers and runs %s with wrapping, alongside Ctrl+Tab", (direction, key, label, legacy, current, target) => {
    const id = `chat.tab.${direction}.bracket`;
    const binding = registeredKeybindings().find((b) => b.id === id);
    expect(binding).toMatchObject({
      label: `${label} chat tab (bracket)`, group: "Chat",
      defaultKeys: `mod+${key}`, scope: "chat", allowInInput: true,
    });
    const snapshot = chatStore.getSnapshot();
    const sessions = ["first", "middle", "last"].map((id) => ({
      id, title: id, messages: [], createdAt: 0, updatedAt: 0,
    }));
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({
      ...snapshot, sessions, currentSessionId: sessions[current].id,
    });
    const switchSession = vi.spyOn(chatStore, "switchSession").mockImplementation(() => {});
    for (const combo of [`mod+${key}`, legacy]) {
      expect(resolve(combo)).toBeNull();
      for (const editable of [false, true]) {
        const hit = resolve(combo, ["chat"], editable);
        expect(hit?.id).toBe(combo === legacy ? `chat.tab.${direction}` : id);
        hit!.run(new KeyboardEvent("keydown"));
        expect(switchSession).toHaveBeenLastCalledWith(sessions[target].id);
      }
    }
    useKeybindingOverrides.getState().setBinding(id, "mod+shift+x");
    expect(resolve(`mod+${key}`, ["chat"], true)).toBeNull();
    expect(resolve("mod+shift+x", ["chat"], true)?.id).toBe(id);
    expect(resolve(legacy, ["chat"], true)?.id).toBe(`chat.tab.${direction}`);
  });

  it.each(["MacIntel", "Win32", "Linux x86_64"])("normalizes bracket keydowns and displays readable combos on %s", async (platform) => {
    vi.resetModules();
    vi.stubGlobal("navigator", { platform, userAgent: platform });
    const { eventToCombo, formatCombo } = await import("./combo");
    const mac = platform === "MacIntel";
    for (const key of ["[", "]"]) {
      const combo = eventToCombo(new KeyboardEvent("keydown", { key, metaKey: mac, ctrlKey: !mac }));
      expect(combo).toBe(`mod+${key}`);
      expect(formatCombo(combo)).toBe(mac ? `⌘${key}` : `Ctrl+${key}`);
    }
  });
});
