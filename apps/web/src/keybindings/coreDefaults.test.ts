// #2949 — ⌘K now clears the chat (the Claude.ai/ChatGPT convention) and the command
// palette moved to ⌘⇧K. These pin the swapped DEFAULTS and how they resolve: the
// chat-scoped clear wins ⌘K inside the chat surface, ⌘K is a no-op elsewhere (the
// palette no longer claims it), and a Settings ▸ Keyboard override still beats both.
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { registeredKeybindings } from "../ext/keybindingRegistry";
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
