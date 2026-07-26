import { describe, expect, it } from "vitest";

import type { Keybinding } from "../ext/keybindingRegistry";
import { resolveBinding } from "./resolve";

// The precedence rules extracted for #1457 so the DOM path and the plugin-iframe path
// share ONE implementation — a chord that behaves differently depending on which pane had
// focus is exactly the bug an operator can't diagnose.

const bind = (over: Partial<Keybinding> & { id: string; defaultKeys: string }): Keybinding => ({
  label: over.id,
  run: () => {},
  ...over,
});

const ctx = (scopes: string[] = [], editable = false) => ({ scopes: new Set(scopes), editable });

describe("resolveBinding", () => {
  it("matches a global binding anywhere", () => {
    const b = bind({ id: "palette", defaultKeys: "mod+k" });
    expect(resolveBinding([b], "mod+k", ctx())?.id).toBe("palette");
  });

  it("returns null when nothing matches the chord", () => {
    expect(resolveBinding([bind({ id: "a", defaultKeys: "mod+k" })], "mod+j", ctx())).toBeNull();
  });

  it("fires a scoped binding only inside its scope", () => {
    const b = bind({ id: "chat.send", defaultKeys: "mod+enter", scope: "chat" });

    expect(resolveBinding([b], "mod+enter", ctx(["chat"]))?.id).toBe("chat.send");
    expect(resolveBinding([b], "mod+enter", ctx(["settings"]))).toBeNull();
  });

  it("lets the scoped binding win over a global one on the same chord", () => {
    const global = bind({ id: "g", defaultKeys: "mod+k" });
    const scoped = bind({ id: "s", defaultKeys: "mod+k", scope: "chat" });

    expect(resolveBinding([global, scoped], "mod+k", ctx(["chat"]))?.id).toBe("s");
    // …and the global still wins when that panel isn't focused.
    expect(resolveBinding([global, scoped], "mod+k", ctx())?.id).toBe("g");
  });

  it("suppresses a binding while focus is in a text field unless it opts in", () => {
    const plain = bind({ id: "slash", defaultKeys: "/" });
    const optedIn = bind({ id: "tab", defaultKeys: "ctrl+tab", allowInInput: true });

    expect(resolveBinding([plain], "/", ctx([], true))).toBeNull();
    expect(resolveBinding([optedIn], "ctrl+tab", ctx([], true))?.id).toBe("tab");
  });

  it("a chord forwarded from a plugin iframe can only reach GLOBAL bindings", () => {
    // The focus chain lives inside the iframe, so the host cannot know which of ITS
    // panels is focused — firing another panel's scoped chord would be worse than
    // not firing. The iframe path passes an empty scope set, which encodes that.
    const scoped = bind({ id: "chat.send", defaultKeys: "mod+enter", scope: "chat" });
    const global = bind({ id: "palette", defaultKeys: "mod+k" });

    expect(resolveBinding([scoped, global], "mod+enter", ctx())).toBeNull();
    expect(resolveBinding([scoped, global], "mod+k", ctx())?.id).toBe("palette");
  });
});
