// The trust rules of the plugin context-menu bridge (#3030). The parser is where a
// sandboxed page's claim becomes host state, so these pin the same guarantees the
// keybinding bridge makes: ids forced into the plugin's namespace, malformed entries
// dropped without costing the batch, nothing executable crossing the boundary.
import { describe, expect, it } from "vitest";

import {
  parsePluginMenuItems,
  parsePluginMenuOpen,
  parsePluginMenuRegistration,
  type PluginMenuEntry,
  type PluginMenuItem,
} from "./pluginContextMenu";

const items = (entries: PluginMenuEntry[] | null): PluginMenuItem[] =>
  (entries ?? []).filter((e): e is PluginMenuItem => !("divider" in e));

describe("parsePluginMenuItems — namespacing and shape", () => {
  it("forces every id into the plugin's namespace and echoes the page's own id back", () => {
    const parsed = items(parsePluginMenuItems([{ id: "copy-id", label: "Copy ID" }], "boardy"));
    expect(parsed).toEqual([
      { id: "plugin.boardy.copy-id", pluginLocalId: "copy-id", label: "Copy ID" },
    ]);
  });

  it("refuses the whole batch with no plugin id — there'd be no namespace to force into", () => {
    expect(parsePluginMenuItems([{ id: "x", label: "X" }], "")).toBeNull();
  });

  it("cannot shadow a core menu item: `configure` becomes the plugin's own `configure`", () => {
    // The rail menu's real Configure… is `configure` (contextMenu/registrations.tsx). A page
    // claiming that id gets its own namespaced entry, so the core item is never replaced —
    // and the two can't collide even inside one merged menu.
    const [it0] = items(parsePluginMenuItems([{ id: "configure", label: "Take over" }], "boardy"));
    expect(it0.id).toBe("plugin.boardy.configure");
    expect(it0.id).not.toBe("configure");
  });

  it("keeps two plugins' identical ids apart", () => {
    const a = items(parsePluginMenuItems([{ id: "open", label: "Open" }], "boardy"))[0];
    const b = items(parsePluginMenuItems([{ id: "open", label: "Open" }], "docs"))[0];
    expect(a.id).not.toBe(b.id);
  });

  it("drops a malformed entry rather than failing the batch", () => {
    const parsed = items(
      parsePluginMenuItems(
        [
          null,
          "not an object",
          { label: "no id" },
          { id: "  ", label: "blank id" },
          { id: "bad id!", label: "illegal chars" },
          { id: "good", label: "Good" },
        ],
        "boardy",
      ),
    );
    expect(parsed.map((e) => e.pluginLocalId)).toEqual(["good"]);
  });

  it("dedupes by id — first declaration wins, like the registry's own dedup", () => {
    const parsed = items(
      parsePluginMenuItems(
        [
          { id: "open", label: "First" },
          { id: "open", label: "Second" },
        ],
        "boardy",
      ),
    );
    expect(parsed).toHaveLength(1);
    expect(parsed[0].label).toBe("First");
  });

  it("falls back to the id for a missing label and truncates a runaway one", () => {
    const parsed = items(
      parsePluginMenuItems([{ id: "open" }, { id: "long", label: "x".repeat(500) }], "boardy"),
    );
    expect(parsed[0].label).toBe("open");
    expect(parsed[1].label).toHaveLength(120);
  });

  it("caps the batch — a view declaring hundreds of entries is a bug or an attack", () => {
    const many = Array.from({ length: 200 }, (_, i) => ({ id: `i${i}`, label: `Item ${i}` }));
    expect(items(parsePluginMenuItems(many, "boardy"))).toHaveLength(32);
  });

  it("takes an icon NAME and only a name — markup and urls are dropped", () => {
    const parsed = items(
      parsePluginMenuItems(
        [
          { id: "a", icon: "clipboard" },
          { id: "b", icon: "LineChart" },
          { id: "c", icon: "<svg onload=alert(1)>" },
          { id: "d", icon: "https://evil.example/x.svg" },
          { id: "e", icon: 42 },
        ],
        "boardy",
      ),
    );
    expect(parsed.map((e) => e.icon)).toEqual(["clipboard", "LineChart", undefined, undefined, undefined]);
  });

  it("carries danger/disabled only as real booleans", () => {
    const parsed = items(
      parsePluginMenuItems(
        [
          { id: "a", danger: true, disabled: true },
          { id: "b", danger: "yes", disabled: 1 },
        ],
        "boardy",
      ),
    );
    expect(parsed[0]).toMatchObject({ danger: true, disabled: true });
    expect(parsed[1].danger).toBeUndefined();
    expect(parsed[1].disabled).toBeUndefined();
  });

  it("collapses leading, trailing and repeated dividers so a menu never opens on a separator", () => {
    const parsed = parsePluginMenuItems(
      [
        { divider: true },
        { id: "a", label: "A" },
        { divider: true },
        { divider: true },
        { id: "b", label: "B" },
        { divider: true },
      ],
      "boardy",
    );
    expect((parsed ?? []).map((e) => ("divider" in e ? "—" : e.pluginLocalId))).toEqual(["a", "—", "b"]);
  });

  it("gives dividers unique ids — the renderer keys on them", () => {
    const parsed = parsePluginMenuItems(
      [{ id: "a" }, { divider: true }, { id: "b" }, { divider: true }, { id: "c" }],
      "boardy",
    );
    const ids = (parsed ?? []).map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("returns null for a non-array items field", () => {
    expect(parsePluginMenuItems(undefined, "boardy")).toBeNull();
    expect(parsePluginMenuItems({ id: "a" }, "boardy")).toBeNull();
  });
});

describe("parsePluginMenuRegistration — the declared default set", () => {
  it("parses a registration", () => {
    const parsed = parsePluginMenuRegistration(
      { type: "protoagent:contextmenu:register", items: [{ id: "copy", label: "Copy" }] },
      "boardy",
    );
    expect(items(parsed).map((e) => e.id)).toEqual(["plugin.boardy.copy"]);
  });

  it("ignores every other message type", () => {
    for (const type of ["protoagent:subscribe", "protoagent:contextmenu:open", "protoagent:publish"]) {
      expect(parsePluginMenuRegistration({ type, items: [{ id: "a" }] }, "boardy")).toBeNull();
    }
    expect(parsePluginMenuRegistration(null, "boardy")).toBeNull();
  });

  it("treats an empty array as a CLEAR, not as junk", () => {
    // Distinct from null (not a registration at all): the host must be able to tell
    // "this view dropped its menu" from "this message wasn't for me".
    expect(parsePluginMenuRegistration({ type: "protoagent:contextmenu:register", items: [] }, "boardy")).toEqual([]);
  });
});

describe("parsePluginMenuOpen — a right-click the page reported", () => {
  const open = (extra: Record<string, unknown>) =>
    parsePluginMenuOpen({ type: "protoagent:contextmenu:open", ...extra }, "boardy");

  it("carries the page's cursor position and its own items", () => {
    const req = open({ x: 120, y: 40, items: [{ id: "copy", label: "Copy" }] });
    expect(req).toMatchObject({ x: 120, y: 40 });
    expect(items(req!.entries).map((e) => e.pluginLocalId)).toEqual(["copy"]);
  });

  it("signals `use the registered set` when items are omitted — distinct from an empty menu", () => {
    expect(open({ x: 1, y: 2 })!.entries).toBeNull();
    expect(open({ x: 1, y: 2, items: [] })!.entries).toEqual([]);
  });

  it("falls back to the frame's top-left on a missing or nonsense coordinate", () => {
    // The operator asked for a menu; a slightly mispositioned one beats none.
    expect(open({})).toMatchObject({ x: 0, y: 0 });
    expect(open({ x: Number.NaN, y: Number.POSITIVE_INFINITY })).toMatchObject({ x: 0, y: 0 });
    expect(open({ x: -50, y: "80" })).toMatchObject({ x: 0, y: 0 });
  });

  it("ignores every other message type, and refuses with no plugin namespace", () => {
    expect(parsePluginMenuOpen({ type: "protoagent:keydown", x: 1, y: 1 }, "boardy")).toBeNull();
    expect(parsePluginMenuOpen({ type: "protoagent:contextmenu:open", x: 1, y: 1 }, "")).toBeNull();
  });
});
