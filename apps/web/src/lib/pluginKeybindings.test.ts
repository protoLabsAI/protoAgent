import { describe, expect, it } from "vitest";

import { parseForwardedKey, parsePluginKeybindings } from "./pluginKeybindings";

// #1457: a plugin view is a sandboxed iframe, so keys pressed inside it never reach the
// host, and the page can't reach `registerKeybinding`. These are the trust rules of the
// bridge that closes both directions.

describe("parsePluginKeybindings", () => {
  it("namespaces a plugin's binding ids under its own plugin id", () => {
    const specs = parsePluginKeybindings(
      { type: "protoagent:keybindings", bindings: [{ id: "toggle", label: "Toggle docs", keys: "mod+shift+d" }] },
      "docs",
    );

    expect(specs).toEqual([
      {
        id: "plugin.docs.toggle",
        pluginLocalId: "toggle",
        label: "Toggle docs",
        group: "Plugins",
        defaultKeys: "mod+shift+d",
      },
    ]);
  });

  it("cannot register or replace a core binding", () => {
    // The whole point of forcing the namespace: a page claiming `chat.new` must not be
    // able to hijack (or silently shadow) the core chord.
    const specs = parsePluginKeybindings(
      { type: "protoagent:keybindings", bindings: [{ id: "chat.new", keys: "mod+n" }] },
      "evil",
    );

    expect(specs?.[0].id).toBe("plugin.evil.chat.new");
    expect(specs?.[0].id).not.toBe("chat.new");
  });

  it("refuses the whole batch when there is no plugin id to namespace into", () => {
    expect(
      parsePluginKeybindings({ type: "protoagent:keybindings", bindings: [{ id: "x", keys: "mod+k" }] }, ""),
    ).toBeNull();
  });

  it("drops a malformed entry without failing the batch", () => {
    const specs = parsePluginKeybindings(
      {
        type: "protoagent:keybindings",
        bindings: [
          { id: "good", keys: "mod+1" },
          { id: "", keys: "mod+2" }, // no id
          { id: "nokeys" }, // no chord
          { id: "bad id!", keys: "mod+3" }, // id charset
          "nonsense",
        ],
      },
      "docs",
    );

    expect(specs?.map((s) => s.pluginLocalId)).toEqual(["good"]);
  });

  it("keeps the first declaration when a page repeats an id", () => {
    const specs = parsePluginKeybindings(
      {
        type: "protoagent:keybindings",
        bindings: [
          { id: "dup", keys: "mod+1", label: "first" },
          { id: "dup", keys: "mod+2", label: "second" },
        ],
      },
      "docs",
    );

    expect(specs).toHaveLength(1);
    expect(specs?.[0].label).toBe("first");
  });

  it("caps how many bindings one view can declare", () => {
    const bindings = Array.from({ length: 100 }, (_, i) => ({ id: `k${i}`, keys: `mod+${i}` }));

    expect(parsePluginKeybindings({ type: "protoagent:keybindings", bindings }, "docs")).toHaveLength(32);
  });

  it("treats an empty array as 'clear my bindings', not as malformed", () => {
    expect(parsePluginKeybindings({ type: "protoagent:keybindings", bindings: [] }, "docs")).toEqual([]);
  });

  it("normalizes the chord and defaults label + group", () => {
    const specs = parsePluginKeybindings(
      { type: "protoagent:keybindings", bindings: [{ id: "go", keys: "  MOD+Shift+G  " }] },
      "docs",
    );

    expect(specs?.[0].defaultKeys).toBe("mod+shift+g");
    expect(specs?.[0].label).toBe("go");
    expect(specs?.[0].group).toBe("Plugins");
  });

  it("ignores messages that aren't a keybinding registration", () => {
    expect(parsePluginKeybindings({ type: "protoagent:subscribe", patterns: [] }, "docs")).toBeNull();
    expect(parsePluginKeybindings({ type: "protoagent:keybindings" }, "docs")).toBeNull();
    expect(parsePluginKeybindings(null, "docs")).toBeNull();
    expect(parsePluginKeybindings("nope", "docs")).toBeNull();
  });
});

describe("parseForwardedKey", () => {
  it("parses a chord the page didn't handle", () => {
    expect(parseForwardedKey({ type: "protoagent:keydown", combo: "mod+k" })).toEqual({
      combo: "mod+k",
      editable: false,
    });
  });

  it("carries the page's claim that focus is in one of its own inputs", () => {
    // So a global chord doesn't fire out from under someone typing in a plugin's search box.
    expect(parseForwardedKey({ type: "protoagent:keydown", combo: "mod+k", editable: true })?.editable).toBe(
      true,
    );
  });

  it("normalizes case and whitespace", () => {
    expect(parseForwardedKey({ type: "protoagent:keydown", combo: " MOD+K " })?.combo).toBe("mod+k");
  });

  it("defaults editable to false for an older kit that omits it", () => {
    expect(parseForwardedKey({ type: "protoagent:keydown", combo: "mod+k", editable: "yes" })?.editable).toBe(
      false,
    );
  });

  it("rejects a non-keydown or empty combo", () => {
    expect(parseForwardedKey({ type: "protoagent:keydown", combo: "   " })).toBeNull();
    expect(parseForwardedKey({ type: "protoagent:keydown" })).toBeNull();
    expect(parseForwardedKey({ type: "protoagent:publish", combo: "mod+k" })).toBeNull();
    expect(parseForwardedKey(undefined)).toBeNull();
  });
});
