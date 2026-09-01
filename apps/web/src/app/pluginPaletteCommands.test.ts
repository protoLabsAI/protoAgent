// ADR 0057 §3/§4 — the trusted adapter that compiles a plugin's declarative `commands:`
// into palette rows. These tests pin the two things that make it safe to run manifest data:
//
//   1. the CLOSED action vocabulary — every type `_parse_commands` emits gets a `run(ctx)`,
//      and anything else (a type the parser rejects, a half-declared entry, a chain that
//      loops) produces NO ROW rather than a row that fires something its author didn't
//      write, and never a throw;
//   2. the namespace RE-VALIDATION — the console mirrors the backend's route/topic checks
//      instead of assuming they ran. That is not belt-and-braces: `apiUrl()` passes an
//      absolute URL through unchanged and the operator bearer is attached, so a route that
//      escapes `/api/plugins/<id>/` is an authenticated write, not a blank iframe. The
//      tab/CR cases below are the bypass the backend fix had to close — the WHATWG URL
//      parser DELETES those characters before it resolves `..`, so a validator reading the
//      declared string sees no dot-dot segment and `fetch` requests one.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Command, PaletteContext, PaletteRegistry } from "@protolabsai/ui/command-palette";

import type { PluginCommand, RuntimeStatus } from "../lib/types";
import {
  compilePluginCommands,
  pluginCommandGroups,
  pluginCommandSources,
  pluginEventTopic,
  pluginRoutePath,
} from "./pluginPaletteCommands";
import type { PluginCommandDeps, PluginCommandSource } from "./pluginPaletteCommands";
import { usePaletteRegistry } from "./usePaletteRegistry";

const TAB = "\t";
const CR = "\r";

// ── Fixtures ─────────────────────────────────────────────────────────────────────

type MockedDeps = PluginCommandDeps & {
  navigate: ReturnType<typeof vi.fn>;
  request: ReturnType<typeof vi.fn>;
  notify: ReturnType<typeof vi.fn>;
};

function makeDeps(over: Partial<PluginCommandDeps> = {}): MockedDeps {
  return {
    inlineViewIds: new Set<string>(),
    navigate: vi.fn(),
    request: vi.fn(async () => {}),
    notify: vi.fn(),
    ...over,
  } as unknown as MockedDeps;
}

function makeSource(commands: PluginCommand[], over: Partial<PluginCommandSource> = {}): PluginCommandSource {
  return {
    id: "files",
    name: "Files",
    commands,
    loaded: true,
    viewIds: new Set(["browser"]),
    icon: () => null,
    ...over,
  };
}

function makeCtx() {
  const ctx = {
    enter: vi.fn(),
    back: vi.fn(),
    close: vi.fn(),
    props: undefined,
  };
  return ctx as unknown as PaletteContext & typeof ctx;
}

const run = (rows: Command[], id: string, ctx: PaletteContext) => {
  const row = rows.find((r) => r.id === id);
  expect(row, `no row ${id}`).toBeTruthy();
  row!.run(ctx);
};

// ── Namespace re-validation: routes ──────────────────────────────────────────────

describe("pluginRoutePath", () => {
  it("composes a relative route under the plugin's own namespace", () => {
    expect(pluginRoutePath("files", "reindex")).toBe("/api/plugins/files/reindex");
    expect(pluginRoutePath("files", "search/all")).toBe("/api/plugins/files/search/all");
    // A `.` segment and a trailing slash normalize exactly as posixpath does.
    expect(pluginRoutePath("files", "./reindex")).toBe("/api/plugins/files/reindex");
    expect(pluginRoutePath("files", "reindex/")).toBe("/api/plugins/files/reindex");
  });

  it("rejects a route that leaves the namespace", () => {
    for (const bad of [
      "",
      "   ",
      "..",
      "../config",
      "../../api/config",
      "sub/../../config",
      "/api/config", // absolute
      "//evil.example.com/api", // protocol-relative
      "https://evil.example.com/api/config",
      "http://evil.example.com/x",
      "localhost/api/config",
      "host:8080/api",
      "sub\\..\\config", // backslash
      "reindex?force=1",
      "reindex#frag",
    ]) {
      expect(pluginRoutePath("files", bad), bad).toBeNull();
    }
    // A route that is not a string at all: coercing `42` into a live segment would be
    // guessing at a payload nothing shaped.
    expect(pluginRoutePath("files", 42 as unknown as string)).toBeNull();
  });

  it("decodes EVERY layer of percent-encoding before checking", () => {
    // One pass turns %252e%252e into %2e%2e, which the browser decodes again into `..`.
    expect(pluginRoutePath("files", "%2e%2e/config")).toBeNull();
    expect(pluginRoutePath("files", "%252e%252e/config")).toBeNull();
    expect(pluginRoutePath("files", "%25252e%25252e/%25252e%25252e/config")).toBeNull();
    // A malformed escape means we cannot know what the browser will request → refuse.
    expect(pluginRoutePath("files", "%zz")).toBeNull();
  });

  it("rejects the tab/CR-inside-a-dot-segment bypass", () => {
    // `new URL("/api/plugins/files/.<TAB>./.<TAB>./config").pathname === "/api/config"` —
    // the URL parser strips tab/LF/CR BEFORE collapsing `..`, so a naive `".." in segments`
    // check reads a string the request never uses. Raw AND percent-encoded.
    expect(pluginRoutePath("files", `.${TAB}./.${TAB}./config`)).toBeNull();
    expect(pluginRoutePath("files", `.${CR}./.${CR}./config`)).toBeNull();
    expect(pluginRoutePath("files", ".%09./.%09./config")).toBeNull();
    expect(pluginRoutePath("files", ".%0d./.%0a./config")).toBeNull();
    expect(pluginRoutePath("files", "%2e%09%2e/%2e%09%2e/config")).toBeNull();
    // …and any other control character, which is never legitimate in a route.
    expect(pluginRoutePath("files", "re\u0000index")).toBeNull();
  });

  it("rejects an unsafe plugin id, so the namespace root itself cannot escape", () => {
    // Without this the root a composed path is checked AGAINST would escape too, and the
    // startsWith assertion would pass while pointing at /api/.
    expect(pluginRoutePath("..", "config")).toBeNull();
    expect(pluginRoutePath("../..", "config")).toBeNull();
    expect(pluginRoutePath("", "config")).toBeNull();
    expect(pluginRoutePath("a/b", "config")).toBeNull();
  });
});

// ── Namespace re-validation: emit topics ─────────────────────────────────────────

describe("pluginEventTopic", () => {
  it("forces a bare name into the plugin's namespace and keeps an own-namespace topic", () => {
    expect(pluginEventTopic("files", "indexed")).toBe("files.indexed");
    expect(pluginEventTopic("files", "files.indexed")).toBe("files.indexed");
    expect(pluginEventTopic("files", "files.index.done")).toBe("files.index.done");
  });

  it("rejects a topic that would forge another plugin's events", () => {
    // The bus check on /api/events/publish never asks WHO is publishing.
    for (const bad of ["", "  ", "otherplugin.wipe", "files.*", "#", "files..done", ".done", "files indexed"]) {
      expect(pluginEventTopic("files", bad), bad).toBeNull();
    }
  });
});

// ── The compile step, per action type ────────────────────────────────────────────

describe("compilePluginCommands", () => {
  it("navigate → the serializable NavIntent chokepoint, never the store", () => {
    const deps = makeDeps();
    const rows = compilePluginCommands(
      makeSource([{ id: "open", title: "Open browser", action: { type: "navigate", view: "browser" } }]),
      deps,
    );
    const ctx = makeCtx();
    run(rows, "plugin:files:open", ctx);
    // A direct `useUI.getState()` call is a silent no-op in the launcher window; the intent
    // is what crosses to the main console window.
    expect(deps.navigate).toHaveBeenCalledWith({ kind: "view", id: "plugin:files:browser" });
    expect(ctx.close).toHaveBeenCalled();
  });

  it("open_view → the inline morph when that view is a registered morph target", () => {
    const deps = makeDeps({ inlineViewIds: new Set(["plugin:files:browser"]) });
    const rows = compilePluginCommands(
      makeSource([
        { id: "quick", title: "Quick browse", action: { type: "open_view", view: "browser", inline: true } },
      ]),
      deps,
    );
    const ctx = makeCtx();
    run(rows, "plugin:files:quick", ctx);
    expect(ctx.enter).toHaveBeenCalledWith("plugin:files:browser");
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it("open_view falls back to navigation when the view never opted into the morph", () => {
    // `_parse_commands` cannot see `views[].palette`, so a manifest may legitimately declare
    // open_view against a view that isn't a morph target. `ctx.enter` on an unregistered
    // view id renders null and BLANKS the whole palette — route to the rail instead.
    const deps = makeDeps({ inlineViewIds: new Set<string>() });
    const rows = compilePluginCommands(
      makeSource([
        { id: "quick", title: "Quick browse", action: { type: "open_view", view: "browser", inline: true } },
      ]),
      deps,
    );
    const ctx = makeCtx();
    run(rows, "plugin:files:quick", ctx);
    expect(ctx.enter).not.toHaveBeenCalled();
    expect(deps.navigate).toHaveBeenCalledWith({ kind: "view", id: "plugin:files:browser" });
    expect(ctx.close).toHaveBeenCalled();
  });

  it("navigate/open_view targeting a view the manifest never declared makes no row", () => {
    const rows = compilePluginCommands(
      makeSource([
        { id: "a", title: "A", action: { type: "navigate", view: "somebody-elses" } },
        { id: "b", title: "B", action: { type: "open_view", view: "somebody-elses", inline: true } },
      ]),
      makeDeps(),
    );
    expect(rows).toEqual([]);
  });

  it("tool → an authenticated call on the composed namespace path, then a toast", async () => {
    const deps = makeDeps();
    const rows = compilePluginCommands(
      makeSource([{ id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } }]),
      deps,
    );
    const ctx = makeCtx();
    run(rows, "plugin:files:reindex", ctx);
    expect(deps.request).toHaveBeenCalledWith("/api/plugins/files/reindex", { method: "POST" });
    expect(ctx.close).toHaveBeenCalled(); // the palette must not hang on the request
    await vi.waitFor(() => expect(deps.notify).toHaveBeenCalled());
    expect(deps.notify.mock.calls[0][0]).toMatchObject({ tone: "success", title: "Reindex" });
  });

  it("tool reports a failed call instead of claiming success", async () => {
    const deps = makeDeps({ request: vi.fn(async () => { throw new Error("404 Not Found"); }) });
    const rows = compilePluginCommands(
      makeSource([{ id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } }]),
      deps,
    );
    run(rows, "plugin:files:reindex", makeCtx());
    await vi.waitFor(() => expect(deps.notify).toHaveBeenCalled());
    expect(deps.notify.mock.calls[0][0]).toMatchObject({ tone: "error", message: "404 Not Found" });
  });

  it("tool with an escaping route makes NO ROW — the mirror runs here, not just upstream", () => {
    // Each of these would be dropped by `_parse_commands`; the console re-checks anyway,
    // because a stale runtime status or a hand-edited payload never passed through it.
    for (const route of [
      "../../config",
      "%2e%2e/%2e%2e/config",
      `.${TAB}./.${TAB}./config`,
      ".%09./.%09./config",
      "https://evil.example.com/api/config",
      "/api/config",
    ]) {
      const deps = makeDeps();
      const rows = compilePluginCommands(
        makeSource([{ id: "x", title: "X", action: { type: "tool", route, method: "POST" } }]),
        deps,
      );
      expect(rows, route).toEqual([]);
      expect(deps.request).not.toHaveBeenCalled();
    }
  });

  it("tool with a verb outside the closed set makes no row (a read must not become a write)", () => {
    const rows = compilePluginCommands(
      makeSource([{ id: "x", title: "X", action: { type: "tool", route: "reindex", method: "TRACE" } }]),
      makeDeps(),
    );
    expect(rows).toEqual([]);
  });

  it("emit → publish on the plugin's own namespace, bare names prefixed", async () => {
    const deps = makeDeps();
    const rows = compilePluginCommands(
      makeSource([
        { id: "ping", title: "Ping", action: { type: "emit", topic: "indexed", data: { n: 1 } } },
        { id: "forge", title: "Forge", action: { type: "emit", topic: "otherplugin.wipe" } },
      ]),
      deps,
    );
    expect(rows.map((r) => r.id)).toEqual(["plugin:files:ping"]); // the forgery makes no row
    run(rows, "plugin:files:ping", makeCtx());
    expect(deps.request).toHaveBeenCalledWith("/api/events/publish", {
      method: "POST",
      body: { topic: "files.indexed", data: { n: 1 } },
    });
    await vi.waitFor(() => expect(deps.notify).toHaveBeenCalled());
  });

  it("command → runs the entry it chains to, and only inside this manifest", () => {
    const deps = makeDeps();
    const rows = compilePluginCommands(
      makeSource([
        { id: "alias", title: "Alias", action: { type: "command", command: "reindex" } },
        { id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } },
      ]),
      deps,
    );
    run(rows, "plugin:files:alias", makeCtx());
    expect(deps.request).toHaveBeenCalledWith("/api/plugins/files/reindex", { method: "POST" });
  });

  it("a chain that loops, dangles, or ends on a dropped command makes no row", () => {
    const rows = compilePluginCommands(
      makeSource([
        { id: "a", title: "A", action: { type: "command", command: "b" } },
        { id: "b", title: "B", action: { type: "command", command: "a" } },
        { id: "dangle", title: "Dangle", action: { type: "command", command: "nope" } },
        { id: "toDropped", title: "To dropped", action: { type: "command", command: "dropped" } },
        // dropped: its route escapes, so it compiles to nothing — and so does anything
        // chaining into it. A dead row is exactly what both sides of this seam prevent.
        { id: "dropped", title: "Dropped", action: { type: "tool", route: "../../config", method: "POST" } },
      ]),
      makeDeps(),
    );
    expect(rows).toEqual([]);
  });

  it("skips a malformed or unknown action instead of throwing", () => {
    const deps = makeDeps();
    const entries = [
      { id: "unknown", title: "Unknown", action: { type: "exec", cmd: "rm -rf /" } },
      { id: "nullish", title: "Nullish", action: null },
      { id: "stringy", title: "Stringy", action: "navigate" },
      { id: "typeless", title: "Typeless", action: {} },
      { id: "actionless", title: "Actionless" },
      // A provider-only entry: parsed + shipped by the backend, but the console does not
      // compile providers yet (ADR 0057 §8 leaves their query budget open), so no row.
      { id: "search", title: "Search", provider: { route: "search", result_action: { type: "navigate", view: "browser" } } },
      { id: "", title: "Unsafe id", action: { type: "navigate", view: "browser" } },
      { id: "../evil", title: "Unsafe id", action: { type: "navigate", view: "browser" } },
      { id: "titleless", title: "", action: { type: "navigate", view: "browser" } },
      { id: "ok", title: "Ok", action: { type: "navigate", view: "browser" } },
    ] as unknown as PluginCommand[];
    let rows: Command[] = [];
    expect(() => {
      rows = compilePluginCommands(makeSource(entries), deps);
    }).not.toThrow();
    expect(rows.map((r) => r.id)).toEqual(["plugin:files:ok"]);
  });

  it("keeps the FIRST of a duplicated command id — label AND action, like the parser does", () => {
    // The two entries dispatch differently on purpose: a row labelled by one entry and
    // wired to another's action is the "half a command" state, not a cosmetic slip.
    const deps = makeDeps();
    const rows = compilePluginCommands(
      makeSource([
        { id: "go", title: "First", action: { type: "navigate", view: "browser" } },
        { id: "go", title: "Second", action: { type: "tool", route: "wipe", method: "DELETE" } },
      ]),
      deps,
    );
    expect(rows.map((r) => r.label)).toEqual(["First"]);
    run(rows, "plugin:files:go", makeCtx());
    expect(deps.navigate).toHaveBeenCalledWith({ kind: "view", id: "plugin:files:browser" });
    expect(deps.request).not.toHaveBeenCalled();
  });

  it("resolves a chain of any length — the mirror stops at a REVISIT, not a depth", () => {
    // The backend resolves chains to a fixed point with no length cap, so a mirror that
    // gave up at some depth would drop a row the parser kept.
    const deps = makeDeps();
    const hops: PluginCommand[] = Array.from({ length: 40 }, (_, i) => ({
      id: `hop${i}`,
      title: `Hop ${i}`,
      action: { type: "command", command: i === 39 ? "reindex" : `hop${i + 1}` },
    }));
    const rows = compilePluginCommands(
      makeSource([
        ...hops,
        { id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } },
      ]),
      deps,
    );
    run(rows, "plugin:files:hop0", makeCtx());
    expect(deps.request).toHaveBeenCalledWith("/api/plugins/files/reindex", { method: "POST" });
  });

  it("a `tool` row on a plugin that never LOADED ships disabled, saying why", () => {
    // Enabled is not loaded: a missing dep or a bad import leaves the plugin with no
    // routers, so its own route 404s. The DS greys a `disabled` row and refuses to run it,
    // which beats firing an authenticated call that can only fail — and beats hiding the
    // row, which reads as "this plugin never shipped it" (the Fleet Room convention).
    const deps = makeDeps();
    const rows = compilePluginCommands(
      makeSource(
        [
          { id: "reindex", title: "Reindex", hint: "the whole tree", action: { type: "tool", route: "reindex", method: "POST" } },
          { id: "alias", title: "Alias", action: { type: "command", command: "reindex" } },
          // Neither of these depends on the plugin's own process: `emit` publishes on the
          // core bus route, and the view host surfaces the loader's real error itself.
          { id: "ping", title: "Ping", action: { type: "emit", topic: "indexed" } },
          { id: "open", title: "Open browser", action: { type: "navigate", view: "browser" } },
        ],
        { loaded: false },
      ),
      deps,
    );
    const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
    expect(byId["plugin:files:reindex"]).toMatchObject({ disabled: true, hint: "plugin not loaded" });
    // A chain into it inherits the reason — an alias for an unrunnable tool is unrunnable.
    expect(byId["plugin:files:alias"]).toMatchObject({ disabled: true, hint: "plugin not loaded" });
    expect(byId["plugin:files:ping"].disabled).toBe(false);
    expect(byId["plugin:files:open"].disabled).toBe(false);
    // Loaded: the manifest's own hint is back and the row runs.
    const live = compilePluginCommands(
      makeSource([
        { id: "reindex", title: "Reindex", hint: "the whole tree", action: { type: "tool", route: "reindex", method: "POST" } },
      ]),
      deps,
    );
    expect(live[0]).toMatchObject({ disabled: false, hint: "the whole tree" });
  });

  it("drops an entry whose title is not a string rather than labelling a row with it", () => {
    // The payload is JSON off a status response, not something the parser is guaranteed to
    // have shaped — a non-string field has no honest reading.
    const rows = compilePluginCommands(
      makeSource([
        { id: "numeric", title: 42, action: { type: "navigate", view: "browser" } },
        { id: "ok", title: "Ok", hint: { evil: true }, group: 7, action: { type: "navigate", view: "browser" } },
      ] as unknown as PluginCommand[]),
      makeDeps(),
    );
    expect(rows.map((r) => r.id)).toEqual(["plugin:files:ok"]);
    expect(rows[0].hint).toBeUndefined();
    expect(rows[0].group).toBe("Plugins"); // not `7`, which would open a junk heading
  });

  it("groups a row with the plugin's other palette presence and keeps it findable", () => {
    const rows = compilePluginCommands(
      makeSource([
        { id: "a", title: "A", hint: "by name", keywords: ["find"], action: { type: "navigate", view: "browser" } },
        { id: "b", title: "B", group: "Files", action: { type: "navigate", view: "browser" } },
      ]),
      makeDeps(),
    );
    // Default group is the plugin section, NOT the generic "Commands" bucket…
    expect(rows[0]).toMatchObject({ id: "plugin:files:a", label: "A", hint: "by name", group: "Plugins" });
    // …and a manifest may name its own section.
    expect(rows[1].group).toBe("Files");
    // The plugin id + name ride the keywords, so typing either finds the row.
    expect(rows[0].keywords).toEqual(expect.arrayContaining(["find", "files", "Files", "plugin"]));
  });
});

// ── Derivation from runtime status ───────────────────────────────────────────────

describe("pluginCommandSources", () => {
  const enabledPlugin = (over: Record<string, unknown> = {}) =>
    ({
      id: "files",
      name: "Files",
      enabled: true,
      loaded: true,
      tools: [],
      skills: 0,
      views: [{ id: "browser", label: "Files", path: "/plugins/files/browser" }],
      commands: [{ id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } }],
      ...over,
    }) as unknown as NonNullable<RuntimeStatus["plugins"]>[number];

  it("derives one source per enabled plugin that declares commands", () => {
    const sources = pluginCommandSources([enabledPlugin()], () => null);
    expect(sources).toHaveLength(1);
    expect(sources[0]).toMatchObject({ id: "files", name: "Files", loaded: true });
    expect([...sources[0].viewIds]).toEqual(["browser"]);
  });

  it("carries the plugin's LOAD state through, so a tool row can disable itself", () => {
    // Enabled-but-not-loaded is a real, common state (a missing pip dep); the compile step
    // needs it to tell a runnable row from one that can only 404.
    const sources = pluginCommandSources([enabledPlugin({ loaded: false })], () => null);
    expect(sources[0].loaded).toBe(false);
  });

  it("contributes NOTHING for a disabled plugin", () => {
    // `loader.py` emits `"commands": []` when a plugin isn't enabled, so this is normally
    // moot — the point is that the console cannot be talked into a row by a payload that
    // says otherwise (a stale status, a hand-edited response). Install != enable != trust.
    expect(pluginCommandSources([enabledPlugin({ enabled: false, commands: [] })], () => null)).toEqual([]);
    expect(
      pluginCommandSources(
        [
          enabledPlugin({
            enabled: false,
            commands: [{ id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } }],
          }),
        ],
        () => null,
      ),
    ).toEqual([]);
  });

  it("ignores a plugin with no commands, an unsafe id, or a missing list", () => {
    expect(pluginCommandSources([enabledPlugin({ commands: [] })], () => null)).toEqual([]);
    expect(pluginCommandSources([enabledPlugin({ commands: undefined })], () => null)).toEqual([]);
    expect(pluginCommandSources([enabledPlugin({ id: "../evil" })], () => null)).toEqual([]);
    expect(pluginCommandSources(undefined, () => null)).toEqual([]);
  });
});

describe("pluginCommandGroups", () => {
  it("stamps each plugin as its rows' attribution source and drops empty plugins", () => {
    const groups = pluginCommandGroups(
      [
        makeSource([{ id: "go", title: "Go", action: { type: "navigate", view: "browser" } }]),
        makeSource([{ id: "x", title: "X", action: { type: "tool", route: "../../config", method: "POST" } }], {
          id: "notes",
          name: "Notes",
        }),
      ],
      makeDeps(),
    );
    expect(groups).toHaveLength(1); // the all-dropped plugin registers nothing
    expect(groups[0].source).toEqual({ id: "plugin:files", label: "Files" });
    expect(groups[0].commands.map((c) => c.id)).toEqual(["plugin:files:go"]);
  });

  it("orders registrations so each section is CONTIGUOUS, default first", () => {
    // Registration order is display order and the DS opens a heading only when the group
    // changes — so a manifest naming its own section must not split the default one, or
    // "Plugins" appears twice with the custom section wedged between.
    const groups = pluginCommandGroups(
      [
        makeSource([
          { id: "own", title: "Own section", group: "Bookmarks", action: { type: "navigate", view: "browser" } },
          { id: "def", title: "Default", action: { type: "navigate", view: "browser" } },
        ]),
        makeSource([{ id: "def", title: "Default", action: { type: "navigate", view: "browser" } }], {
          id: "notes",
          name: "Notes",
        }),
      ],
      makeDeps(),
    );
    // Every row, in the order the host registers them: the default section (which the
    // plugin VIEW rows already opened) completes before the named one starts.
    expect(groups.flatMap((g) => g.commands.map((c) => `${c.group}/${c.id}`))).toEqual([
      "Plugins/plugin:files:def",
      "Plugins/plugin:notes:def",
      "Bookmarks/plugin:files:own",
    ]);
    // Still one registration per plugin per section, so each keeps its own chip.
    expect(groups.map((g) => g.source.label)).toEqual(["Files", "Notes", "Files"]);
  });
});

// ── Reaching the DS palette ──────────────────────────────────────────────────────
// The compile step is only half the contract: the rows also have to arrive in the DS
// registry the palette actually reads, in the plugin section, carrying the chip. Mounting
// the hook is the only way to see that — the same reason `paletteSourceProvider.test.ts`
// mounts rather than testing its factory alone.

let root: Root | null = null;

beforeEach(() => {
  // The fleet roster poll would otherwise hit the network on every mount; hanging it keeps
  // the fleet data undefined, a state the hook already handles.
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
});

afterEach(() => {
  root?.unmount();
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

async function mountRegistry(sources: PluginCommandSource[]): Promise<PaletteRegistry> {
  let registry: PaletteRegistry | null = null;
  const Probe = () => {
    registry = usePaletteRegistry([], [], undefined, { sources, notify: () => {} });
    return null;
  };
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  root.render(h(QueryClientProvider, { client }, h(Probe)));
  await vi.waitFor(() => expect(registry).not.toBeNull());
  return registry!;
}

describe("usePaletteRegistry — plugin-declared commands", () => {
  it("registers the compiled rows with their plugin as the DS source chip", async () => {
    const registry = await mountRegistry([
      makeSource([{ id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } }]),
    ]);
    await vi.waitFor(() => {
      const row = registry.getStaticCommands().find((c) => c.id === "plugin:files:reindex");
      expect(row).toBeTruthy();
      expect(row!.group).toBe("Plugins");
      expect(row!.source).toEqual({ id: "plugin:files", label: "Files" });
    });
  });

  it("contributes nothing when the host passes no plugin commands", async () => {
    const registry = await mountRegistry([]);
    await vi.waitFor(() => expect(registry.getStaticCommands().length).toBeGreaterThan(0));
    expect(registry.getStaticCommands().filter((c) => c.id.startsWith("plugin:"))).toEqual([]);
  });
});
