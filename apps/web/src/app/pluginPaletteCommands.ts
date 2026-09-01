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
// write), and the same goes for a malformed or unknown action type. The one row that ships
// without running is a `tool` on a plugin that is enabled but never LOADED: it has no route
// to call, so the row is disabled and says why (`NOT_LOADED`) rather than vanishing.
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
// ADJACENT to the plugin's own view rows, since the palette's root view opens a group
// heading wherever a row's group differs from the row above it, and the seam's statics are
// registered at the end of the list. So the
// host registers these on the DS registry directly, exactly as it already does for a
// plugin's nav rows — the ADR 0057 §4 sketch's `registry.registerCommands(cmds, {source})`.
import type { ReactNode } from "react";
import type { Command, PaletteContext, PaletteSource } from "@protolabsai/ui/command-palette";
import type { PluginCommand, PluginCommandAction, RuntimeStatus } from "../lib/types";
import { isNavigablePluginView } from "../lib/pluginViews";
// Straight at the nav chokepoint's own module rather than the `usePaletteRegistry` barrel:
// type-only either way (erased at build), but `palette/registry.ts` imports the compiler
// below, so going through the barrel would draw a cycle on the module graph for no reason.
import type { NavIntent } from "./palette/nav";

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

/** A manifest string, or "" — coerced, never guessed at. The payload is JSON off a status
 *  response, so a field that is not a string is a field this adapter has no honest reading
 *  of: for a title or a route that means no row at all, for the decorative fields it means
 *  absent. */
function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

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
  const declared = text(route);
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
  const declared = text(topic);
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
  /** Did the plugin actually LOAD? Enabled is not loaded: a missing dep, a bad import or
   *  a mount race leaves an enabled plugin with no routers, which is exactly the state
   *  `views[].pluginLoaded` exists to explain on the view host. A `tool` row needs it
   *  (see `NOT_LOADED`). */
  loaded: boolean;
  /** View ids this manifest declares that the console mounts as a NAVIGABLE surface — a
   *  `navigate`/`open_view` may target no other. Declared is not navigable: a `slot: "chat"`
   *  claimant renders under the core chat id and a `utility` widget is a bottom-left pill,
   *  so neither is reconciled onto a dock (`isNavigablePluginView`). Targeting one used to
   *  compile a live "go to" row that set a surface id nothing renders, which App's
   *  stale-surface fallback answered by yanking the operator to chat. */
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
  // `Array.isArray`, not `?? []`: this runs during App's render, so a `commands` that is
  // truthy-but-not-a-list (a stale status, a hand-edited response — the same payload this
  // module refuses to trust with a route) would throw on `.map` and land on the console's
  // ROOT error boundary. A malformed list contributes no rows; it does not replace the
  // whole console with the crash card.
  const list = Array.isArray(plugins) ? plugins : [];
  return list
    .filter((p) => p.enabled && Array.isArray(p.commands) && p.commands.length && SAFE_SLUG.test(p.id))
    .map((p) => ({
      id: p.id,
      name: p.name || p.id,
      commands: p.commands ?? [],
      loaded: !!p.loaded,
      // The NAVIGABLE subset, through the same predicate App's rail and the launcher use —
      // not the raw declared list. Filtering here, at the single place the allow-set is
      // built, is what makes `compileAction`'s existing `viewIds.has(...)` guard drop the
      // row: this module's own answer to "there is nothing honest to run".
      viewIds: new Set(
        (Array.isArray(p.views) ? p.views : []).filter(isNavigablePluginView).map((v) => String(v?.id)),
      ),
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

/** The section a row lands in unless its manifest names another — the SAME heading the
 *  plugin's view rows register under, so a plugin's commands continue its existing palette
 *  section instead of opening a generic "Commands" bucket underneath it. */
const DEFAULT_GROUP = "Plugins";

/** Headings the CONSOLE already renders, which a manifest may not claim. Plugin rows
 *  register between the plugin nav rows and the Commands group (`palette/registry.ts`), so a
 *  manifest naming one of these would put a SECOND "Agents"/"Commands" heading in the middle
 *  of the list — the exact duplicate-heading failure `pluginCommandGroups` orders its
 *  registrations to avoid. A collision falls back to the plugin's own section rather than
 *  being dropped: the row is fine, only its placement was. ("Plugins" is absent on purpose —
 *  a manifest naming it is asking for the default, which is where it already goes.) */
const CORE_SECTIONS = new Set(["Agents", "Commands"]);

/** Why a compiled row is present but not runnable, said out loud on the row.
 *
 *  Only `tool` needs it: that route is served by the PLUGIN's own router, so an enabled
 *  plugin that failed to load (missing dep, bad import, mount race) has nothing mounted and
 *  the row could only 404 into an error toast. `emit` publishes on the core bus route and
 *  `navigate`/`open_view` reach the view host, which surfaces the loader's real diagnostic
 *  itself (`views[].pluginError`) — those stay live. Disabling rather than hiding is the
 *  Fleet Room command's convention in `usePaletteRegistry`: a row that explains itself is
 *  discoverable, a row that vanishes reads as "this plugin never shipped it". */
const NOT_LOADED = "plugin not loaded";

const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

/** The palette row id for a plugin command — namespaced by the adapter, per ADR 0057 §3
 *  ("adapter namespaces → plugin:<id>:search"). */
function rowId(pluginId: string, commandId: string): string {
  return `plugin:${pluginId}:${commandId}`;
}

/** The section a row renders under: the manifest's own, unless it named nothing or named a
 *  heading the console already owns (see `CORE_SECTIONS`). */
function sectionFor(declared: string): string {
  return !declared || CORE_SECTIONS.has(declared) ? DEFAULT_GROUP : declared;
}

/** A compiled action: the `run(ctx)`, the plain word for what it DOES, and — when the row
 *  is present but not runnable — the reason it says out loud. A record rather than flags
 *  baked into `run` so that a chain INHERITS both: an alias for a tool on a plugin that
 *  never loaded is exactly as unrunnable as the tool it points at, and it navigates or
 *  fires for the same reason its target does. */
type Compiled = { run: (ctx: PaletteContext) => void; verb?: string; unavailable?: string };

/** The words an operator types for the two things a row can do. They have to be ON the row
 *  to be typeable at all — the DS matches a query against `label + hint + group +
 *  source.label + keywords` and nothing else. "go to" is this console's existing word for a
 *  row that opens a surface: the plugin VIEW rows beside these already hint it and `Open…`
 *  keywords it, so a plugin-declared `navigate` that didn't would be the one navigation row
 *  in the palette that "go to" misses. "open" is deliberately NOT used — `Open…` owns that
 *  term, and every plugin row matching it would bury the command the operator meant. An
 *  inline morph gets no word at all, matching its sibling view row: it happens in place, so
 *  there is nowhere to "go". */
const GO_TO = "go to";
const RUN = "run";

/** Compile ONE action, or `null` when it cannot be dispatched safely. `null` drops the row:
 *  an action type this adapter does not implement, a route that escapes the namespace, a
 *  topic outside the plugin's own, or a view that is not a navigable surface of this plugin
 *  (never declared, or declared as a chat-slot claimant / utility widget — see `viewIds`)
 *  all mean the same thing — there is nothing honest to run. (Not runnable YET — a plugin
 *  that failed to load — is a different answer: that row ships disabled, saying so.) */
function compileAction(
  action: PluginCommandAction | undefined,
  src: PluginCommandSource,
  title: string,
  deps: PluginCommandDeps,
): Compiled | null {
  if (!action || typeof action !== "object") return null;
  const fail = (e: unknown) =>
    deps.notify({ tone: "error", title: `${title} failed`, message: e instanceof Error ? e.message : String(e) });

  switch (action.type) {
    case "navigate": {
      if (!src.viewIds.has(action.view)) return null;
      const viewKey = `plugin:${src.id}:${action.view}`;
      return {
        verb: GO_TO,
        run: (ctx) => {
          deps.navigate({ kind: "view", id: viewKey });
          ctx.close();
        },
      };
    }
    case "open_view": {
      if (!src.viewIds.has(action.view)) return null;
      const viewKey = `plugin:${src.id}:${action.view}`;
      // `ctx.enter` on a view id nobody registered renders `null` — the whole palette BLANKS
      // until Escape pops the frame. A view only becomes an inline morph target by opting in
      // through `views[].palette`, which `_parse_commands` cannot see, so a manifest can
      // legitimately declare `open_view` against a view that never opted in. Route those to
      // the rail instead: the operator still gets the view, just not inside the palette.
      // Decided HERE and not inside `run`, so the word the row SHOWS and the thing it DOES
      // cannot disagree — the host re-registers on the inline set anyway (`inlineSig`), and
      // the sibling plugin view row resolves `inline` at build time for the same reason.
      const inline = deps.inlineViewIds.has(viewKey);
      return {
        verb: inline ? undefined : GO_TO,
        run: (ctx) => {
          if (inline) {
            ctx.enter(viewKey);
            return;
          }
          deps.navigate({ kind: "view", id: viewKey });
          ctx.close();
        },
      };
    }
    case "tool": {
      const method = text(action.method).toUpperCase();
      if (!HTTP_METHODS.has(method)) return null;
      // Composed + asserted HERE, once, so the closure below can only ever hold a path
      // that passed the namespace check — there is no code path from a manifest string to
      // `fetch` that skips this line.
      const path = pluginRoutePath(src.id, action.route);
      if (!path) return null;
      return {
        verb: RUN,
        run: (ctx) => {
          ctx.close();
          deps
            .request(path, { method })
            // "accepted", not "finished": a 2xx says the plugin's route TOOK the call, and a
            // route that kicks off a long job answers 202 the moment it starts. Claiming the
            // work is done would be the one thing this toast can't know.
            .then(() => deps.notify({ tone: "success", title, message: `${src.name} accepted the request.` }))
            .catch(fail);
        },
        // The plugin serves this route itself; if it never loaded, nothing does.
        unavailable: src.loaded ? undefined : NOT_LOADED,
      };
    }
    case "emit": {
      const topic = pluginEventTopic(src.id, action.topic);
      if (!topic) return null;
      const data = action.data && typeof action.data === "object" ? action.data : {};
      return {
        verb: RUN,
        run: (ctx) => {
          ctx.close();
          deps
            .request("/api/events/publish", { method: "POST", body: { topic, data } })
            .then(() => deps.notify({ tone: "success", title, message: `Published ${topic}.` }))
            .catch(fail);
        },
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

/** One entry, normalized the way `_parse_commands` normalizes it — so every pass below
 *  works on strings and cannot be surprised by a payload the parser never shaped. */
type Entry = {
  id: string;
  title: string;
  hint: string;
  icon: string;
  group: string;
  keywords: string[];
  action?: PluginCommandAction;
};

/** Compile one plugin's declared commands into DS palette `Command`s.
 *
 *  Chains are resolved HERE, at compile time, the way the backend resolves them: a
 *  `command` action collapses to the run of the entry it eventually reaches, and an entry
 *  whose chain loops or lands on a command that was itself dropped produces no row. Both
 *  halves matter — a dead row that silently does nothing is exactly the "half a command"
 *  state the strictness on both sides of this seam exists to prevent. */
export function compilePluginCommands(src: PluginCommandSource, deps: PluginCommandDeps): Command[] {
  // Normalize and de-duplicate ONCE, applying the parser's own three gates in its own
  // order (safe id, non-empty title, first of a repeated id wins). Deduping here rather
  // than at the end is what keeps a repeated id honest: a later entry that overwrote the
  // compiled run would ship a row LABELLED by the first and WIRED to the second.
  const seenIds = new Set<string>();
  const entries: Entry[] = (Array.isArray(src.commands) ? src.commands : []).flatMap((c) => {
    if (!c || typeof c !== "object") return [];
    const id = text(c.id);
    const title = text(c.title);
    if (!SAFE_SLUG.test(id) || !title || seenIds.has(id)) return [];
    seenIds.add(id);
    return [
      {
        id,
        title,
        hint: text(c.hint),
        icon: text(c.icon),
        group: sectionFor(text(c.group)),
        keywords: (Array.isArray(c.keywords) ? c.keywords.map(text) : []).filter(Boolean),
        action: c.action,
      },
    ];
  });
  // Pass 1 — everything that dispatches directly.
  const direct = new Map<string, Compiled>();
  const chain = new Map<string, string>();
  for (const c of entries) {
    if (c.action?.type === "command") {
      const target = text(c.action.command);
      if (target) chain.set(c.id, target);
      continue;
    }
    const compiled = compileAction(c.action, src, c.title, deps);
    if (compiled) direct.set(c.id, compiled);
  }
  // Pass 2 — follow each chain to a directly-dispatchable entry. `seen` is the whole
  // termination proof: every hop either lands on one, dangles, or revisits an id, and the
  // id space is finite. Deliberately UNCAPPED, like the backend's fixed-point resolution —
  // a mirror that quietly stopped at some depth would drop a row the parser kept.
  const resolve = (id: string): Compiled | undefined => {
    const seen = new Set<string>();
    let current = id;
    while (!seen.has(current)) {
      seen.add(current);
      const compiled = direct.get(current);
      if (compiled) return compiled;
      const next = chain.get(current);
      if (!next) return undefined; // dangling, or it pointed at a dropped command
      current = next;
    }
    return undefined; // a --> b --> a
  };

  const out: Command[] = [];
  for (const c of entries) {
    const compiled = resolve(c.id);
    if (!compiled) continue;
    out.push({
      id: rowId(src.id, c.id),
      label: c.title,
      // A row that cannot run explains itself and greys out instead of firing into a 404.
      // The reason WINS over the manifest's own hint: on a disabled row, why is the only
      // trailing text worth the space. Failing both, the row says what it does — the same
      // "go to" the plugin's view rows carry, so a nav row still reads as one when its
      // author wrote no hint.
      hint: compiled.unavailable || c.hint || compiled.verb,
      disabled: !!compiled.unavailable,
      icon: src.icon(c.icon || undefined),
      // Grouped with the plugin's OTHER palette presence (its view rows) rather than
      // dumped in the generic "Commands" bucket; a manifest may name its own section.
      group: c.group,
      // The plugin's id and name join the manifest's own keywords, so "projectboard" or
      // the chip text finds the row even when the title says neither. The verb rides here
      // too, not only in `hint`: a manifest that wrote its own hint would otherwise be the
      // one row "go to" or "run" can't reach.
      keywords: [...c.keywords, src.id, src.name, "plugin", ...(compiled.verb ? [compiled.verb] : [])],
      run: compiled.run,
    });
  }
  return out;
}

/** Every plugin's rows, each stamped with the plugin as its DS `source` (rendered as the
 *  attribution chip), in the order the host should register them. */
export function pluginCommandGroups(
  sources: PluginCommandSource[],
  deps: PluginCommandDeps,
): { source: PaletteSource; commands: Command[] }[] {
  const compiled = sources
    .map((src) => ({
      source: { id: `plugin:${src.id}`, label: src.name } satisfies PaletteSource,
      commands: compilePluginCommands(src, deps),
    }))
    .filter((g) => g.commands.length > 0);
  // On the EMPTY query — the one list that is grouped at all — registration order is display
  // order (`pickRootFill` selects rows but never reorders them), and a section heading opens
  // wherever a row's group differs from the row above it. So rows sharing a section have to be
  // CONTIGUOUS or its name appears twice. (A TYPED query is ranked across groups and renders
  // no headings at all, so none of this binds there.) A manifest may name its own section, which would
  // otherwise interleave: one plugin's `group: Files` row landing between default rows
  // re-opens "Plugins" underneath it. So the split is per (section, plugin): sections in
  // display order with the default FIRST (it continues the heading the plugin view rows
  // already opened), and each plugin's rows within a section stay ONE registration, which
  // is what stamps that plugin's own chip on them.
  const named = compiled.flatMap((g) => g.commands.map((c) => c.group ?? DEFAULT_GROUP));
  const sections = [DEFAULT_GROUP, ...new Set(named.filter((s) => s !== DEFAULT_GROUP))];
  return sections.flatMap((section) =>
    compiled
      .map((g) => ({ source: g.source, commands: g.commands.filter((c) => c.group === section) }))
      .filter((g) => g.commands.length > 0),
  );
}
