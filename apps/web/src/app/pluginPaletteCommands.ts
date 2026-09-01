// ADR 0057 §3/§4 — the trusted adapter that turns a plugin's DECLARATIVE `commands:`
// manifest block into runnable palette rows.
//
// The architectural rule this module exists to enforce: **the web is the single dispatch
// authority.** A plugin's code never enters the console bundle (that is the entire reason
// commands are manifest data and views are sandboxed iframes), so the only thing that can
// turn `{ type: "tool", route: "reindex", method: "POST" }` into behavior is the compile
// step below. Everything a row can do is spelled out here, in a closed vocabulary that is
// kept in lock-step with what `_parse_commands` (graph/plugins/manifest.py) actually emits:
// implement an action the parser rejects and it is dead code; accept one it never emits and
// you have opened a second, unvalidated dispatch path.
//
// **Re-validation, not trust.** The backend already confines every route to
// `/api/plugins/<id>/…` and every emit topic to the plugin's own event namespace. This
// module mirrors both checks rather than assuming they ran, because the payload it reads is
// just JSON on a `/api/runtime/status` response — a stale cached status, a future parser
// regression, or a hand-edited response are all ways an unchecked route reaches `fetch`.
// That is not a blank iframe: `lib/api.ts` `apiUrl()` passes an absolute `https://…`
// through UNCHANGED, and `applyAuth` attaches the operator bearer — so an escaped route is
// an authenticated write against whatever it names. A route that fails the mirror produces
// NO ROW (half a command is worse than none — a row that fires something its author didn't
// write), and the same goes for a malformed or unknown action type.
//
// Enable-gating is NOT re-implemented here: `loader.py` emits `"commands": []` for a
// plugin that isn't enabled, so a disabled plugin sends nothing to compile. The
// `p.enabled` filter in `pluginCommandSources` mirrors App's view derivation for the same
// stale/hand-edited-payload reason as above, and `pluginPaletteCommands.test.ts` pins the
// no-rows-when-disabled contract from this side too.
//
// **Why these rows do NOT go through `src/ext/paletteRegistry.ts`.** That seam (ADR 0061)
// is the FORK path — build-time, in-process, trusted code a fork compiles into its own
// bundle — and its own header draws exactly this line ("distinct from plugin manifest
// palette views (ADR 0057)"). Plugin manifest data is the other path, and it needs three
// things the fork seam deliberately does not carry: `ctx.enter` (the inline morph), a DS
// `source` stamp (the attribution chip a fork command has no use for), and registration
// ADJACENT to the plugin's own view rows, since the DS opens a new group heading whenever
// the group changes and the seam's statics are registered at the end of the list. So the
// host registers these on the DS registry directly, exactly as it already does for a
// plugin's nav rows — the ADR 0057 §4 sketch's `registry.registerCommands(cmds, {source})`.
import type { ReactNode } from "react";
import type { Command, PaletteContext, PaletteSource } from "@protolabsai/ui/command-palette";
import type { PluginCommand, PluginCommandAction, RuntimeStatus } from "../lib/types";
// Type-only (erased at build): the runtime import would be a cycle, since
// `usePaletteRegistry` imports the compiler below.
import type { NavIntent } from "./usePaletteRegistry";

/** A plugin id namespaces its routes, its config section and its event topics, so it has
 *  to be a safe slug before any of those get composed from it. Mirrors `_VALID_PLUGIN_ID`
 *  / `_VALID_COMMAND_ID`. Load-bearing for the route mirror: with an id of `..`, the
 *  namespace root a composed path is checked AGAINST would itself escape, and the check
 *  would pass while pointing at `/api/`. */
const SAFE_SLUG = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

/** ASCII C0 controls + DEL. The WHATWG URL parser DELETES tab/LF/CR as its very first
 *  step — before it resolves `..` — so `.<TAB>./.<TAB>./config` is not a dot-dot segment
 *  to a validator reading the declared string and IS one to the `fetch` that follows
 *  (`new URL("/api/plugins/evil/.\t./.\t./config").pathname === "/api/config"`). The other
 *  controls are never legitimate in a route either, so a route carrying any of them is
 *  rejected outright rather than sanitized — which is what keeps the string checked here
 *  identical to the string the browser will request. Mirrors `_URL_RESHAPING_CHARS`. */
// eslint-disable-next-line no-control-regex
const URL_RESHAPING_CHARS = /[\u0000-\u001f\u007f]/;

/** A route carrying a scheme, host or port — `apiUrl()` returns an absolute URL verbatim,
 *  so this is the difference between a same-origin plugin call and an authenticated
 *  cross-origin one. Mirrors `_NON_SAME_ORIGIN_PATH`. */
const NON_SAME_ORIGIN = /https?:\/\/|^\/\/|localhost|:\d/i;

/** `value` with EVERY layer of percent-encoding removed — nested on purpose, because a
 *  single decode turns `%252e%252e` into `%2e%2e`, which the browser then decodes again
 *  into `..`. A validator that stops after one pass is inspecting a different string than
 *  the one the request path is matched on. Mirrors `_percent_decoded`, with two
 *  JS-specific refusals: `decodeURIComponent` THROWS on a malformed escape (Python's
 *  `unquote` leaves it alone), and a decode that never reaches a fixed point is
 *  pathological — both mean "we cannot know what the browser will request", so both
 *  reject rather than guess. */
function percentDecoded(value: string): string | null {
  let current = value;
  for (let i = 0; i < 8; i++) {
    let next: string;
    try {
      next = decodeURIComponent(current);
    } catch {
      return null;
    }
    if (next === current) return current;
    current = next;
  }
  return null;
}

/** `posixpath.normpath` for an absolute URL path: collapse `.`/empty segments, pop on
 *  `..`, drop the trailing slash. POSIX rules and not the platform's, because a URL route
 *  stays `/`-separated everywhere — the same reason the backend reaches for `posixpath`
 *  rather than `os.path` on the Windows leg. */
function normalizePath(path: string): string {
  const out: string[] = [];
  for (const segment of path.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      out.pop();
      continue;
    }
    out.push(segment);
  }
  return `/${out.join("/")}`;
}

/** The absolute, same-origin path a `tool`/`provider` route resolves to, or `null` when it
 *  escapes the plugin's namespace (in which case the row is dropped, never "fixed up").
 *
 *  This is the console-side mirror of `_parse_command_route`, and it deliberately re-derives
 *  the composed path from the plugin id rather than checking the route string in isolation:
 *  the guarantee callers need is literally "the thing we are about to `fetch` still starts
 *  with `/api/plugins/<id>/`", so that is the assertion the last line makes. Checks run in
 *  the browser's own order — decode, reject reshaping controls, then normalize `..`. */
export function pluginRoutePath(pluginId: string, route: string): string | null {
  if (!SAFE_SLUG.test(pluginId)) return null;
  const root = `/api/plugins/${pluginId}/`;
  const declared = String(route ?? "").trim();
  const decoded = percentDecoded(declared);
  if (!decoded) return null;
  if (URL_RESHAPING_CHARS.test(decoded)) return null;
  if (NON_SAME_ORIGIN.test(decoded)) return null;
  if (decoded.startsWith("/")) return null;
  if (decoded.includes("\\") || decoded.includes("?") || decoded.includes("#")) return null;
  if (decoded.split("/").includes("..")) return null;
  const composed = normalizePath(root + decoded);
  return composed.startsWith(root) ? composed : null;
}

/** An `emit` topic forced into THIS plugin's event namespace, or `null`. Mirrors
 *  `_parse_command_topic`, and for the same reason it exists server-side: the bus check on
 *  `POST /api/events/publish` (ADR 0039) only asks that a topic be dotted and
 *  wildcard-free — never WHO is publishing — so an unchecked topic could forge another
 *  plugin's events (`otherplugin.wipe`). A bare name is prefixed; a dotted one must already
 *  start with the plugin id. */
export function pluginEventTopic(pluginId: string, topic: string): string | null {
  if (!SAFE_SLUG.test(pluginId)) return null;
  const declared = String(topic ?? "").trim();
  const segments = declared.split(".");
  if (!declared || /\s/.test(declared)) return null;
  if (declared.includes("*") || declared.includes("#")) return null;
  if (segments.some((s) => !s)) return null;
  if (segments.length === 1) return `${pluginId}.${declared}`;
  return segments[0] === pluginId ? declared : null;
}

/** One enabled plugin's palette contribution, derived from runtime status. */
export type PluginCommandSource = {
  /** Plugin id — namespaces the row ids, the routes and the event topics. */
  id: string;
  /** Display name; the DS renders it as the row's `source.label` chip. */
  name: string;
  /** The manifest's declared commands (already enable-gated server-side). */
  commands: PluginCommand[];
  /** View ids this manifest declares — a `navigate`/`open_view` may target no other. */
  viewIds: ReadonlySet<string>;
  /** Resolved icon for a manifest icon name. Injected because the two windows resolve
   *  glyphs differently: App resolves the full lucide set, the launcher deliberately uses
   *  one generic mark rather than pulling that chunk into a secondary surface. */
  icon: (name?: string) => ReactNode;
};

/** Derive every enabled plugin's command contribution from runtime status.
 *
 *  Shared rather than inlined because BOTH windows need it — the console App and the
 *  frameless desktop launcher mount the same palette registry, and the plugin-VIEW
 *  derivation they each already carry is the cautionary tale (App.tsx and Launcher.tsx
 *  hold two hand-kept-in-step copies of it). One function, two callers. */
export function pluginCommandSources(
  plugins: RuntimeStatus["plugins"],
  icon: (name?: string) => ReactNode,
): PluginCommandSource[] {
  return (plugins ?? [])
    .filter((p) => p.enabled && p.commands?.length && SAFE_SLUG.test(p.id))
    .map((p) => ({
      id: p.id,
      name: p.name || p.id,
      commands: p.commands ?? [],
      viewIds: new Set((p.views ?? []).map((v) => String(v.id))),
      icon,
    }));
}

/** What the compiled `run(ctx)` bodies are allowed to reach. Injected rather than imported
 *  so the compile step stays a pure, directly testable function — and so navigation cannot
 *  quietly bypass the palette's chokepoint (below). */
export type PluginCommandDeps = {
  /** Palette view ids registered as inline morph targets (`plugin:<id>:<view>`) — the
   *  `inlineViews` the host handed `usePaletteRegistry`. */
  inlineViewIds: ReadonlySet<string>;
  /** The palette's serializable NavIntent chokepoint. NEVER `useUI.getState()` directly:
   *  the frameless launcher window mounts this same registry in a shell-less JS context
   *  where store mutations are inert, so a direct store call is a silent no-op there —
   *  the intent has to be forwardable to the main window. */
  navigate: (intent: NavIntent) => void;
  /** An authenticated same-origin JSON request; rejects on a non-2xx response. */
  request: (path: string, init: { method: string; body?: unknown }) => Promise<void>;
  /** Transient feedback for a `tool`/`emit` result (the window's `useToast()`). */
  notify: (n: { tone: "success" | "error"; title?: string; message: string }) => void;
};

/** A chain longer than this is a manifest doing something pathological. The backend
 *  already proves every chain terminates inside the plugin's own list, so this is only the
 *  mirror's own stop condition. */
const MAX_CHAIN = 16;

const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

/** The palette row id for a plugin command — namespaced by the adapter, per ADR 0057 §3
 *  ("adapter namespaces → plugin:<id>:search"). */
function rowId(pluginId: string, commandId: string): string {
  return `plugin:${pluginId}:${commandId}`;
}

/** Compile ONE action into a `run(ctx)`, or `null` when it cannot be dispatched safely.
 *  `null` drops the row: an action type this adapter does not implement, a route that
 *  escapes the namespace, a topic outside the plugin's own, or a view the manifest never
 *  declared all mean the same thing — there is nothing honest to run. */
function compileAction(
  action: PluginCommandAction | undefined,
  src: PluginCommandSource,
  title: string,
  deps: PluginCommandDeps,
): ((ctx: PaletteContext) => void) | null {
  if (!action || typeof action !== "object") return null;
  const fail = (e: unknown) =>
    deps.notify({ tone: "error", title: `${title} failed`, message: e instanceof Error ? e.message : String(e) });

  switch (action.type) {
    case "navigate": {
      if (!src.viewIds.has(action.view)) return null;
      const viewKey = `plugin:${src.id}:${action.view}`;
      return (ctx) => {
        deps.navigate({ kind: "view", id: viewKey });
        ctx.close();
      };
    }
    case "open_view": {
      if (!src.viewIds.has(action.view)) return null;
      const viewKey = `plugin:${src.id}:${action.view}`;
      return (ctx) => {
        // `ctx.enter` on a view id nobody registered renders `null` — the whole palette
        // BLANKS until Escape pops the frame. A view only becomes an inline morph target
        // by opting in through `views[].palette`, which `_parse_commands` cannot see, so a
        // manifest can legitimately declare `open_view` against a view that never opted
        // in. Route those to the rail instead: the operator still gets the view, just not
        // inside the palette.
        if (deps.inlineViewIds.has(viewKey)) {
          ctx.enter(viewKey);
          return;
        }
        deps.navigate({ kind: "view", id: viewKey });
        ctx.close();
      };
    }
    case "tool": {
      const method = String(action.method ?? "").trim().toUpperCase();
      if (!HTTP_METHODS.has(method)) return null;
      // Composed + asserted HERE, once, so the closure below can only ever hold a path
      // that passed the namespace check — there is no code path from a manifest string to
      // `fetch` that skips this line.
      const path = pluginRoutePath(src.id, action.route);
      if (!path) return null;
      return (ctx) => {
        ctx.close();
        deps
          .request(path, { method })
          .then(() => deps.notify({ tone: "success", title, message: `${src.name} ran the command.` }))
          .catch(fail);
      };
    }
    case "emit": {
      const topic = pluginEventTopic(src.id, action.topic);
      if (!topic) return null;
      const data = action.data && typeof action.data === "object" ? action.data : {};
      return (ctx) => {
        ctx.close();
        deps
          .request("/api/events/publish", { method: "POST", body: { topic, data } })
          .then(() => deps.notify({ tone: "success", title, message: `Published ${topic}.` }))
          .catch(fail);
      };
    }
    // `command` is resolved by `compilePluginCommands`, which is the only place that can
    // see the sibling entries a chain hops through.
    case "command":
      return null;
    default:
      return null;
  }
}

/** Compile one plugin's declared commands into DS palette `Command`s.
 *
 *  Chains are resolved HERE, at compile time, the way the backend resolves them: a
 *  `command` action collapses to the run of the entry it eventually reaches, and an entry
 *  whose chain loops or lands on a command that was itself dropped produces no row. Both
 *  halves matter — a dead row that silently does nothing is exactly the "half a command"
 *  state the strictness on both sides of this seam exists to prevent. */
export function compilePluginCommands(src: PluginCommandSource, deps: PluginCommandDeps): Command[] {
  const entries = (src.commands ?? []).filter(
    (c): c is PluginCommand => !!c && typeof c === "object" && SAFE_SLUG.test(String(c.id ?? "")) && !!c.title,
  );
  // Pass 1 — everything that dispatches directly.
  const direct = new Map<string, (ctx: PaletteContext) => void>();
  const chain = new Map<string, string>();
  for (const c of entries) {
    if (c.action?.type === "command") {
      const target = String(c.action.command ?? "").trim();
      if (target) chain.set(c.id, target);
      continue;
    }
    const run = compileAction(c.action, src, c.title, deps);
    if (run) direct.set(c.id, run);
  }
  // Pass 2 — follow each chain to a directly-dispatchable entry, refusing loops.
  const resolve = (id: string): ((ctx: PaletteContext) => void) | undefined => {
    const seen = new Set<string>();
    let current = id;
    for (let i = 0; i < MAX_CHAIN; i++) {
      if (seen.has(current)) return undefined; // a --> b --> a
      seen.add(current);
      const run = direct.get(current);
      if (run) return run;
      const next = chain.get(current);
      if (!next) return undefined; // dangling, or it pointed at a dropped command
      current = next;
    }
    return undefined;
  };

  const out: Command[] = [];
  const claimed = new Set<string>();
  for (const c of entries) {
    if (claimed.has(c.id)) continue; // duplicate id — keep the first, like the parser
    const run = resolve(c.id);
    if (!run) continue;
    claimed.add(c.id);
    out.push({
      id: rowId(src.id, c.id),
      label: c.title,
      hint: c.hint || undefined,
      icon: src.icon(c.icon),
      // Grouped with the plugin's OTHER palette presence (its view rows) rather than
      // dumped in the generic "Commands" bucket; a manifest may name its own section.
      group: c.group || "Plugins",
      // The plugin's id and name join the manifest's own keywords, so "projectboard" or
      // the chip text finds the row even when the title says neither.
      keywords: [...(c.keywords ?? []).map(String), src.id, src.name, "plugin"],
      run,
    });
  }
  return out;
}

/** Every plugin's rows, each stamped with the plugin as its DS `source` (rendered as the
 *  attribution chip). Registered per-plugin by the host so the chip is per-source. */
export function pluginCommandGroups(
  sources: PluginCommandSource[],
  deps: PluginCommandDeps,
): { source: PaletteSource; commands: Command[] }[] {
  return sources
    .map((src) => ({
      source: { id: `plugin:${src.id}`, label: src.name } satisfies PaletteSource,
      commands: compilePluginCommands(src, deps),
    }))
    .filter((g) => g.commands.length > 0);
}
