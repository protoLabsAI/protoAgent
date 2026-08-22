// #2949 — ⌘K (mod+k) now clears the chat (the Claude.ai/ChatGPT convention) and the
// command palette moved to ⌘⇧K (mod+shift+k). Pinned through the REAL registered
// bindings and the SAME resolution path the keydown host uses (resolveBinding +
// effectiveCombo), so both the defaults and override precedence are covered.
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import { api } from "../lib/api";
import { chatStore } from "../chat/chat-store";
import { useKbIntents } from "./intents";
import { useKeybindingOverrides } from "./overrides";
import { resolveBinding, type ResolveContext } from "./resolve";

beforeAll(async () => {
  await import("./coreKeybindings"); // registration side effect
});

afterEach(() => {
  useKeybindingOverrides.getState().resetAll();
  vi.restoreAllMocks();
});

const ctx = (scopes: string[] = [], editable = false): ResolveContext => ({
  scopes: new Set(scopes),
  editable,
});

const resolve = (combo: string, c: ResolveContext = ctx()) =>
  resolveBinding(registeredKeybindings(), combo, c);

describe("⌘K clear / ⌘⇧K palette default swap (#2949)", () => {
  it("mod+k clears the chat when focus is in the chat panel — even while typing", () => {
    expect(resolve("mod+k", ctx(["chat"]))?.id).toBe("chat.clear");
    expect(resolve("mod+k", ctx(["chat"], true))?.id).toBe("chat.clear");
  });

  it("mod+k outside the chat panel does nothing — clear stays scoped", () => {
    expect(resolve("mod+k", ctx())).toBeNull();
    expect(resolve("mod+k", ctx(["settings"]))).toBeNull();
  });

  it("mod+shift+k opens the palette from anywhere, including the chat panel", () => {
    expect(resolve("mod+shift+k", ctx())?.id).toBe("palette.toggle");
    expect(resolve("mod+shift+k", ctx(["chat"], true))?.id).toBe("palette.toggle");
    expect(resolve("mod+shift+k", ctx(["settings"]))?.id).toBe("palette.toggle");
  });

  it("palette.toggle's run() flips the palette intent", () => {
    const before = useKbIntents.getState().paletteOpen;
    resolve("mod+shift+k", ctx())!.run(new KeyboardEvent("keydown"));
    expect(useKbIntents.getState().paletteOpen).toBe(!before);
  });

  it("chat.clear does exactly what /clear does: retire the server session, wipe messages", () => {
    const del = vi
      .spyOn(api, "deleteChatSession")
      .mockResolvedValue({ deleted: true, harvested: false });
    const wipe = vi.spyOn(chatStore, "updateMessages").mockImplementation(() => {});
    const { currentSessionId } = chatStore.getSnapshot();
    expect(currentSessionId).toBeTruthy();

    resolve("mod+k", ctx(["chat"]))!.run(new KeyboardEvent("keydown"));

    expect(del).toHaveBeenCalledWith(currentSessionId, false);
    expect(wipe).toHaveBeenCalledWith(currentSessionId, []);
  });

  it("user overrides still beat the new defaults (rebindable, ADR 0063)", () => {
    // A user restores the pre-#2949 layout via Settings ▸ Keyboard…
    useKeybindingOverrides.getState().setBinding("palette.toggle", "mod+k");
    useKeybindingOverrides.getState().setBinding("chat.clear", "mod+shift+k");

    // …and their chords win everywhere, including inside the chat panel.
    expect(resolve("mod+k", ctx())?.id).toBe("palette.toggle");
    expect(resolve("mod+k", ctx(["chat"]))?.id).toBe("palette.toggle");
    expect(resolve("mod+shift+k", ctx(["chat"]))?.id).toBe("chat.clear");
    // The overridden defaults are fully vacated: nothing answers ⌘⇧K outside chat.
    expect(resolve("mod+shift+k", ctx())).toBeNull();
  });
});
