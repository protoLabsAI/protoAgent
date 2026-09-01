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
import { CommandPalette } from "@protolabsai/ui/command-palette";
import type { Command, PaletteContext, PaletteRegistry } from "@protolabsai/ui/command-palette";

import type { PluginCommand, RuntimeStatus } from "../lib/types";
import { buildViews } from "../lib/viewRegistry";
import {
  compilePluginCommands,
  pluginCommandGroups,
  pluginCommandSources,
  pluginEventTopic,
  pluginRoutePath,
} from "./pluginPaletteCommands";
import type { PluginCommandDeps, PluginCommandSource } from "./pluginPaletteCommands";
import { setPaletteNavigator, usePaletteRegistry } from "./usePaletteRegistry";
import type { NavIntent } from "./palette/nav";

// jsdom implements no layout, so it ships no `scrollIntoView` — the palette root scrolls its
// selected row into view. An environment gap, not a behaviour under test.
Element.prototype.scrollIntoView ??= () => {};

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

  it("navigate/open_view at a view with no console SURFACE makes no row either", () => {
    // Declared is not navigable. `pluginCommandSources` hands over only the navigable ids
    // (see its own test below); a chat-slot claimant or a utility widget is therefore absent
    // from `viewIds` and drops the row — rather than compiling a live "go to" that sets a
    // surface id nothing renders, which App answers by yanking the operator to chat.
    const rows = compilePluginCommands(
      makeSource(
        [
          { id: "a", title: "A", action: { type: "navigate", view: "quick" } },
          { id: "b", title: "B", action: { type: "open_view", view: "chatty", inline: true } },
          // A chain onto one is exactly as undispatchable as its target.
          { id: "c", title: "C", action: { type: "command", command: "a" } },
        ],
        { viewIds: new Set(["browser"]) },
      ),
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
    expect(rows[0].hint).toBe("go to"); // the action's own word, not the `{evil: true}` object
    expect(rows[0].group).toBe("Plugins"); // not `7`, which would open a junk heading
  });

  it("says what the row DOES when the manifest wrote no hint, in the words a search uses", () => {
    // The DS matches on `label + hint + group + source.label + keywords`, so the verb has to
    // be ON the row to be typeable at all.
    const deps = makeDeps({ inlineViewIds: new Set(["plugin:files:browser"]) });
    const rows = compilePluginCommands(
      makeSource([
        { id: "go", title: "Browse", action: { type: "navigate", view: "browser" } },
        { id: "morph", title: "Quick browse", action: { type: "open_view", view: "browser", inline: true } },
        { id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } },
        { id: "own", title: "Own words", hint: "the whole tree", action: { type: "tool", route: "reindex", method: "POST" } },
      ]),
      deps,
    );
    const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
    // The word the plugin's VIEW rows already hint — otherwise a plugin-declared navigation
    // row is the one navigation row in the palette that typing "go to" misses.
    expect(byId["plugin:files:go"].hint).toBe("go to");
    // An inline morph happens in place: nowhere to "go", exactly like its sibling view row.
    expect(byId["plugin:files:morph"].hint).toBeUndefined();
    expect(byId["plugin:files:morph"].keywords).not.toContain("go to");
    expect(byId["plugin:files:reindex"].hint).toBe("run");
    // A manifest's own hint always wins the visible slot…
    expect(byId["plugin:files:own"].hint).toBe("the whole tree");
    // …and the verb still rides the keywords, or that row is the one "run" cannot find.
    expect(byId["plugin:files:own"].keywords).toContain("run");
  });

  it("sends a manifest group that claims a console heading back to the plugin section", () => {
    // These rows register between the plugin nav rows and the Commands group, so a manifest
    // naming "Agents" or "Commands" would open a SECOND heading by that name mid-list — the
    // duplicate-heading failure the registration order exists to prevent. The row is fine;
    // only its placement was, so it lands in the plugin's own section rather than dropping.
    const rows = compilePluginCommands(
      makeSource([
        { id: "a", title: "A", group: "Commands", action: { type: "navigate", view: "browser" } },
        { id: "b", title: "B", group: "Agents", action: { type: "navigate", view: "browser" } },
        { id: "c", title: "C", group: "Bookmarks", action: { type: "navigate", view: "browser" } },
      ]),
      makeDeps(),
    );
    expect(rows.map((r) => r.group)).toEqual(["Plugins", "Plugins", "Bookmarks"]);
  });

  it("compiles nothing from a `commands` that is not a list, rather than throwing", () => {
    // Same premise as the route mirror: this is JSON off a status response, and it is read
    // inside a render — a throw here replaces the whole console with the crash card.
    expect(compilePluginCommands(makeSource("reindex" as unknown as PluginCommand[]), makeDeps())).toEqual([]);
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

  it("offers only the views the console mounts as a SURFACE — not every declared one", () => {
    // App's rail and the launcher both derive their surfaces as `allDeclaredViews` MINUS the
    // chat-slot claimant and the utility widgets (`isNavigablePluginView`). This allow-set
    // has to be the same list: a `navigate` at a view with no surface compiled a row that
    // read "go to", set `plugin:files:quick` as the surface, found no panel — and App's
    // stale-surface fallback dropped the operator on chat, off whatever they were looking at.
    const sources = pluginCommandSources(
      [
        enabledPlugin({
          views: [
            { id: "browser", label: "Files", path: "/plugins/files/browser" },
            { id: "panel", label: "Panel", path: "/plugins/files/panel", placement: "right" },
            { id: "quick", label: "Quick", path: "/plugins/files/quick", utility: true },
            { id: "info", label: "Info", path: "/plugins/files/info", utility: { info: "hi" } },
            { id: "chatty", label: "Chat", path: "/plugins/files/chatty", slot: "chat" },
          ],
        }),
      ],
      () => null,
    );
    // A right-dock view IS a surface (railOrder reconciles rail/right/bottom alike).
    expect([...sources[0].viewIds].sort()).toEqual(["browser", "panel"]);
  });

  it("compiles NO row for a declared command whose view has no surface (end to end)", () => {
    // The two halves above, joined: the derivation the hosts actually call, feeding the
    // compile step. This is the shipped path — the unit cases can both pass while the wiring
    // between them hands over the raw list.
    const deps = makeDeps();
    const sources = pluginCommandSources(
      [
        enabledPlugin({
          views: [{ id: "quick", label: "Quick", path: "/plugins/files/quick", utility: true }],
          commands: [{ id: "q", title: "Quick capture", action: { type: "navigate", view: "quick" } }],
        }),
      ],
      () => null,
    );
    expect(compilePluginCommands(sources[0], deps)).toEqual([]);
    expect(deps.navigate).not.toHaveBeenCalled();
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

  it("survives a status payload that is not shaped like one", () => {
    // This runs inside App's render, so a `.filter`/`.map` on a truthy non-list would land on
    // the console's ROOT error boundary — the whole app replaced by the crash card because one
    // plugin's status field was the wrong type.
    expect(pluginCommandSources("nope" as unknown as RuntimeStatus["plugins"], () => null)).toEqual([]);
    expect(pluginCommandSources([enabledPlugin({ commands: "reindex" })], () => null)).toEqual([]);
    // A malformed `views` costs the plugin its navigate rows, not the console.
    const sources = pluginCommandSources([enabledPlugin({ views: "browser" })], () => null);
    expect([...sources[0].viewIds]).toEqual([]);
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

// The hook takes ADR 0056's whole View facade now (`{ views, viewFor }`), not a bare array.
// Empty on purpose: these tests are about the PLUGIN rows, and a core surface list would
// only add noise to `getStaticCommands()`.
const EMPTY_VIEWS = buildViews({ core: [], plugins: [], ext: [] });

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

// Sources arrive as a PROP, and the component identity stays fixed, so `rerender` is a real
// re-render of the same mounted hook — the only way to see the effect's re-register/withdraw
// path, which a fresh mount would hide.
function Probe({
  sources,
  onRegistry,
}: {
  sources: PluginCommandSource[];
  onRegistry: (r: PaletteRegistry) => void;
}) {
  onRegistry(usePaletteRegistry(EMPTY_VIEWS, [], undefined, { sources, notify: () => {} }));
  return null;
}

type Mounted = { registry: PaletteRegistry; rerender: (sources: PluginCommandSource[]) => void };

async function mountRegistry(sources: PluginCommandSource[]): Promise<Mounted> {
  let registry: PaletteRegistry | null = null;
  const host = document.createElement("div");
  document.body.appendChild(host);
  const mounted = createRoot(host);
  root = mounted;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rerender = (next: PluginCommandSource[]) =>
    mounted.render(
      h(QueryClientProvider, { client }, h(Probe, { sources: next, onRegistry: (r) => (registry = r) })),
    );
  rerender(sources);
  await vi.waitFor(() => expect(registry).not.toBeNull());
  return { registry: registry!, rerender };
}

describe("usePaletteRegistry — plugin-declared commands", () => {
  it("registers the compiled rows with their plugin as the DS source chip", async () => {
    const { registry } = await mountRegistry([
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
    const { registry } = await mountRegistry([]);
    await vi.waitFor(() => expect(registry.getStaticCommands().length).toBeGreaterThan(0));
    expect(registry.getStaticCommands().filter((c) => c.id.startsWith("plugin:"))).toEqual([]);
  });

  it("re-registers when a plugin's manifest changes and withdraws rows it stops declaring", async () => {
    // The effect keys on a HAND-BUILT signature of the compile inputs (`cmdSig`) — the status
    // array's identity churns on every 3s poll, so it cannot be the dependency. Nothing else in
    // this file would notice a field missing from that string: every compile test would still
    // pass while the live palette showed rows from a manifest ago.
    const source = makeSource(
      [{ id: "reindex", title: "Reindex", action: { type: "tool", route: "reindex", method: "POST" } }],
      { loaded: false },
    );
    const { registry, rerender } = await mountRegistry([source]);
    const row = (id: string) => registry.getStaticCommands().find((c) => c.id === id);
    await vi.waitFor(() => expect(row("plugin:files:reindex")).toMatchObject({ disabled: true }));
    // The plugin finishes loading — same id, same title, only `loaded` moved. Drop it from the
    // signature and the row stays greyed out until something unrelated re-registers.
    rerender([{ ...source, loaded: true }]);
    await vi.waitFor(() => expect(row("plugin:files:reindex")).toMatchObject({ disabled: false }));
    // A manifest that stops declaring a command withdraws its row (the effect's cleanup runs on
    // every re-register), rather than leaving a stale one that still fires the old route.
    rerender([{ ...source, loaded: true, commands: [{ id: "other", title: "Other", action: { type: "emit", topic: "ping" } }] }]);
    await vi.waitFor(() => expect(row("plugin:files:other")).toBeTruthy());
    expect(row("plugin:files:reindex")).toBeUndefined();
  });
});

// ── The late-arrival case: plugin rows landing under the operator's arrow ─────────
//
// Reviewers raised this on both palette PRs: arm a second command source and ⌘K resets the
// highlight to row 0, so Enter runs whatever is first instead of what you arrowed to. It was
// real, and it was the DS `commandsView`'s defect — that view resets selection on
// `[filtered.length]`, which is index-keyed and therefore blind to a re-rank that keeps the
// count or inserts a row above the selection. #3289 replaced it with a host-owned root that
// keys selection on the COMMAND ID, so the fix is INHERITED here rather than re-implemented.
//
// Inherited is exactly why the test belongs on this side of the seam. Plugin rows are the
// console's own late arrival — runtime status resolves after mount, and `cmdSig` re-registers
// them on every manifest change — and nothing in `palette/rootView.test.ts` mounts the real
// adapter. Move these rows onto a provider, or re-key the effect so a re-register sweeps the
// registry, and every test in that file would still pass while ⌘K went back to running the
// wrong command.
describe("plugin rows arriving late never steal the operator's selection", () => {
  const intents: NavIntent[] = [];

  beforeEach(() => {
    // The palette's nav chokepoint, captured instead of applied — so "which row did Enter
    // actually run?" is one exact payload rather than an inspection of the UI store.
    intents.length = 0;
    setPaletteNavigator((i) => intents.push(i));
  });
  afterEach(() => setPaletteNavigator(null));

  function PalettePane({ sources }: { sources: PluginCommandSource[] }) {
    const registry = usePaletteRegistry(EMPTY_VIEWS, [], undefined, { sources, notify: () => {} });
    return h(CommandPalette, { open: true, onOpenChange: () => {}, registry });
  }

  const rowLabels = () =>
    [...document.querySelectorAll('[role="option"] .pl-cmdk-commands__label')].map((n) => n.textContent);
  const selectedLabel = () =>
    document.querySelector('[data-sel="true"] .pl-cmdk-commands__label')?.textContent ?? null;
  const input = () => document.querySelector<HTMLInputElement>(".pl-cmdk-commands__input")!;

  function type(value: string) {
    const el = input();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  const press = (key: string) =>
    input().dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));

  async function mountPalette(sources: PluginCommandSource[]) {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const mounted = createRoot(host);
    root = mounted;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const rerender = (next: PluginCommandSource[]) =>
      mounted.render(h(QueryClientProvider, { client }, h(PalettePane, { sources: next })));
    rerender(sources);
    await vi.waitFor(() => expect(document.querySelector(".pa-cmdk")).not.toBeNull());
    return rerender;
  }

  it("keeps the highlight — and Enter — on the row the operator arrowed to", async () => {
    // No plugin sources yet: this is the console a beat before runtime status lands.
    const rerender = await mountPalette([]);
    type("settings");
    // Asserted on the ROWS THIS CASE STEERS BY, not the whole matched list. "settings" matches
    // the generated `Settings: <Section>` row for every section of the dialog (#3291) — 22 of
    // them and growing with the section table — so an exact full-list assertion here would be
    // pinned to a corpus this case does not care about and would re-break on the next section
    // anyone adds. What it does care about: `Settings` ranks first (exact beats prefix), and
    // the row it arrows onto is the one it later presses Enter on.
    await vi.waitFor(() => expect(rowLabels()[0]).toBe("Settings"));
    expect(rowLabels()).toContain("Settings: Fleet");
    press("ArrowDown");
    // The list settles asynchronously (the root view's provider read), so wait for the move
    // rather than reading straight after the keypress.
    await vi.waitFor(() => expect(selectedLabel()).not.toBe("Settings"));
    const picked = selectedLabel()!; // it really moved off row 0

    // Status lands. The new row is a PREFIX match registered ahead of the deep links, so it
    // ranks in ABOVE the selection — the selected command keeps its identity and loses its
    // index, which is precisely what an index-keyed reset cannot survive.
    rerender([
      makeSource([{ id: "sync", title: "Settings Sync", action: { type: "emit", topic: "sync" } }]),
    ]);
    await vi.waitFor(() => expect(rowLabels()).toContain("Settings Sync"));
    // The new row ranks ABOVE the selection, so the selected command keeps its identity and
    // loses its index — precisely what an index-keyed reset cannot survive.
    expect(rowLabels().indexOf("Settings Sync")).toBeLessThan(rowLabels().indexOf(picked));
    expect(selectedLabel()).toBe(picked);
    // `aria-activedescendant` is the only thing a screen reader has to go on, so it has to
    // have followed the row too — a highlight that moved without it is half a fix.
    expect(input().getAttribute("aria-activedescendant")).toBe(
      document.querySelector('[data-sel="true"]')!.id,
    );

    // The whole point: Enter runs what was selected. Row 0 (`Settings`) would push
    // `{ kind: "global" }` with no section, so this payload distinguishes the two outcomes.
    // The whole point: Enter runs what was SELECTED, not row 0. Row 0 (`Settings`) pushes
    // `{ kind: "global" }` with no section, so an intent carrying a section proves the
    // selection survived — and it must be the section of the row actually highlighted.
    press("Enter");
    expect(intents).toHaveLength(1);
    expect(intents[0]).toMatchObject({ kind: "global" });
    // A section at all means it was NOT row 0 — `Settings` emits `{ kind: "global" }` bare.
    expect((intents[0] as { section?: string }).section).toBeTruthy();
    expect(picked).toMatch(/^Settings: /);
  });
});
