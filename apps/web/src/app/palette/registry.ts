// ADR 0057 — the command-palette adapter. Feeds the DS palette registry from the console's
// existing sources, organized to be command-driven rather than a flat list of places.
//
// The ROOT VIEW IS OURS (`rootView.tsx`, upstream gap protoLabsAI/protoContent#503), which
// changes what this adapter has to supply. It no longer hands the DS one flat list and hopes
// registration order reads well; it hands over two:
//
//   • the ROOT corpus — registered commands, what the empty palette shows (recents first,
//     then this, capped). Short by construction: Agents -> Plugins -> Commands.
//   • the SEARCH-ONLY corpus — every built-in/fork surface, admitted the moment the operator
//     types. This is the fix for the defect that motivated the whole PR: typing "memory" or
//     "knowledge" — the names of two rail surfaces — used to render "No matches", because
//     the surfaces existed ONLY inside the `Open` submorph's private command list. They are
//     now searchable from the root without flooding it, which is a split only a host-owned
//     view can express (`matchCommand` returns true for the empty query).
//
// The same surface list still backs the `Open` submorph, so browsing by hand is unchanged.
// Inline plugin views (a plugin view that opts in via `views[].palette: "inline"`) are
// registered as DS `pluginView()`s — their command morphs the palette body into the plugin's
// own iframe (themed/authed via the handshake) instead of navigating to its rail.
import type { ReactNode } from "react";
import { createElement, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { Download, Keyboard, PanelsTopLeft, Settings2, Store, Users } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { commandsView, createPaletteRegistry, pluginView } from "@protolabsai/ui/command-palette";
import type {
  Command,
  CommandProvider,
  PaletteRegistry,
  PaletteView,
} from "@protolabsai/ui/command-palette";
import { useQuery } from "@tanstack/react-query";

import type { View, buildViews } from "../../lib/viewRegistry";
import {
  hasPaletteSources,
  paletteCommandsVersion,
  registerPaletteCommand,
  subscribePaletteCommands,
  visiblePaletteCommands,
} from "../../ext/paletteRegistry";
import type { PaletteCommand } from "../../ext/paletteRegistry";
import { registeredKeybindings } from "../../ext/keybindingRegistry";
import { formatCombo } from "../../keybindings/combo";
import { effectiveCombo, useKeybindingOverrides } from "../../keybindings/overrides";
import { useFlagPredicate } from "../../flags/flags";
import { apiUrl, authToken, isHostConsole } from "../../lib/api";
import { chatCommandsQuery, fleetQuery, runtimeStatusQuery } from "../../lib/queries";
import { chatStore } from "../../chat/chat-store";
import { chatPaletteSignature, chatSlashPaletteRows } from "../chatSlashPalette";
import { markAgentOpened } from "../fleetPalette";
import { fleetRoomView } from "../FleetRoom";
import { fleetSettingsDisabledReason } from "../fleetSettingsGate";
import { memberDmView } from "../PaletteChat";
import { settingsPaletteCommands } from "../settingsPalette";
import { pluginCommandGroups } from "../pluginPaletteCommands";
import type { PluginCommandDeps, PluginCommandSource } from "../pluginPaletteCommands";
import { registerKeybindingCommands, SHORTCUT_KEYWORDS } from "./keybindingCommands";
import { navigate } from "./nav";
import type { NavIntent } from "./nav";
import { matchCommand } from "./rank";
import { markAgentUsed, markCommandUsed } from "./recents";
import { paletteRootView } from "./rootView";
import type { RootViewConfig } from "./rootView";
import { knowledgeSearchProvider } from "./knowledgeSearch";

/** Optional inline chat with the focused agent (ADR 0057). App builds the native chat
 *  PaletteView (it needs JSX + the focused agent name); the adapter registers it + an
 *  "Ask <agent>" command that morphs into it. */
export type PaletteChatConfig = {
  name: string;
  icon?: ReactNode;
  view: PaletteView;
};

/** A plugin view opted into inline morphing (`views[].palette: "inline"`). Carries
 *  everything the DS `pluginView()` needs to mount + run the handshake. */
export type InlinePluginView = {
  /** `plugin:<id>:<view>` — matches the view's nav id, so the command can `enter()` it. */
  id: string;
  title: string;
  /** Slug-aware resolved page URL (`apiUrl(view.path)`). */
  url: string;
  icon?: ReactNode;
  /** The console theme payload (`consoleTheme()`) — the curated six keys plus the
   *  full `--pl-*` snapshot and `mode` (#2225) — posted on init. */
  theme: Record<string, string>;
  /** Operator bearer (`authToken()`) for the page's same-origin authed calls. */
  token: string;
  sandbox?: string;
};

/** ADR 0056's unified-View façade, whole: the addressable handles AND the by-id resolver.
 *  Taking the façade rather than a bare `View[]` is what finally gives `viewFor` a consumer
 *  (both call sites used to destructure `views` and throw the resolver away) — see
 *  `resolveSurfaces` for where the palette actually resolves through it. */
export type ViewsFacade = ReturnType<typeof buildViews>;

// Core deep-link palette commands — DOGFOODED through the public `registerPaletteCommand`
// seam (ADR 0061), so core uses the same path a fork does (no bypass). Each deep-link is a
// serializable NavIntent routed through `navigate()`, so it works identically in the console
// window (apply locally) and the desktop launcher (forward to the main window). The sub-tab
// ids are the uiStore union types (source of truth), so they can't drift into a 404.
// (Inbox moved to a utility-bar widget; Schedule is a top-level rail surface that
// is searchable as a "go to" surface — so no Activity deep-links here.)

// The row is a flex line — `[icon] label … hint` with NO icon gutter — so an icon-less row's
// label sits flush left while an icon'd row's is indented past the glyph. That cost nothing
// while every core row went without one; the generated Settings rows below carry the Settings
// rail's glyphs, which would otherwise leave a visible step mid-group. So the hand-written
// rows take the glyph their DESTINATION already wears: the utility-bar Settings pill
// (Settings2), the Plugins tabs (Store), the Install-from-URL button (Download), and the two
// morph commands. `createElement` because this module is a .ts, not a .tsx.
const glyph = (Icon: LucideIcon) => createElement(Icon, { size: 18 });

/** One core deep-link row: registered through the public seam, its `run` a NavIntent. The
 *  row is spelled as FIELDS rather than positionals — it has grown an icon and now a
 *  keybinding, and five positional arguments of which two are a ReactNode and a string[]
 *  is a miscall a reader can't see and the types won't catch. `group` is fixed here: every
 *  core deep-link lands in Commands, beside the generated Settings rows. */
const _link = (cmd: Omit<PaletteCommand, "run" | "group">, intent: NavIntent) =>
  registerPaletteCommand({
    ...cmd,
    group: "Commands",
    run: (ctx) => {
      navigate(intent);
      ctx.close();
    },
  });
// REGISTRATION ORDER IS DISPLAY ORDER on the empty query (recents, then this list under a
// per-group quota), so the most-wanted deep-link goes FIRST. `Settings` leads: it is the
// only row here that is a destination in its own right rather than a shortcut into one, and
// on a first run — no recency, nothing to promote it — whichever rows are registered last
// are the rows an operator never sees. It used to sit third, behind both plugin deep-links.
//
// Settings is the consolidated dialog now (2026-06) — opened from the utility-bar pill, the
// drawer, or these palette commands. This one opens it wherever it was left; the generated
// per-section rows below open a named pane, and they are registered LAST on purpose: 22 rows
// must not crowd the empty-query root out from under the three that lead it. They cost the
// root nothing (the per-group quota caps it) and everything they add lands on SEARCH.
//
// The bare row ADVERTISES the `settings.open` binding (ADR 0063) instead of leaving its hint
// empty or hard-coding "⌘,": the chord is rebindable in Settings ▸ Keyboard, and the seam
// renders `formatCombo(effectiveCombo(binding))`, so a literal would start lying the moment
// an operator rebound it. It also stops this row being the one blank right edge in a group
// where the generated rows all carry one — and teaches the shortcut at the moment someone is
// reaching for Settings the slow way. In the desktop Launcher window the hint is simply
// absent: only App.tsx pulls the keybindings barrel, so nothing has registered
// `settings.open` there and `toDsCommand` finds no binding. That degradation is the right one
// — the chord isn't live in that window either, and importing the binding host for a cosmetic
// hint is most of what keeping the Launcher lean bought.
_link(
  {
    id: "settings",
    label: "Settings",
    icon: glyph(Settings2),
    keybinding: "settings.open",
    keywords: ["settings", "config", "preferences", "options"],
  },
  { kind: "global" },
);
_link(
  {
    id: "plug:market",
    label: "Plugins: Discover",
    icon: glyph(Store),
    keywords: ["plugins", "discover", "market", "directory", "browse"],
  },
  { kind: "plugins", tab: "market" },
);
// Install-from-URL is the advanced action under Installed now (ADR 0059 D4) — land there.
_link(
  {
    id: "plug:download",
    label: "Plugins: Install from URL",
    icon: glyph(Download),
    keywords: ["plugins", "install", "url", "git"],
  },
  { kind: "plugins", tab: "local" },
);
// …and one row per Settings SECTION, generated from the section table rather than hand-listed
// here. Three sections used to be reachable from ⌘K (the bare "Settings" above, plus
// hand-written `box:fleet` / `box:telemetry` rows these supersede) and the other twenty were
// not — a list nobody remembered to extend, failing silently when they didn't.
// `settingsPaletteCommands` derives them from settings/sections.ts, so coverage follows the
// table. (`settings:telemetry` also picks up the `hostOnly` gate the hand-written row never
// had, so a member window stops listing a pane it cannot open.)
//
// Registered UNCONDITIONALLY, gates and all: each row carries the section's own
// `flag`/`hostOnly` as DATA, and `visiblePaletteCommands` applies them per render below. That
// ordering is the point — resolving a flag HERE, at module load, would read the fail-closed
// answer `/api/flags` hasn't returned yet and hide Secrets/Devices/Publish permanently.
for (const cmd of settingsPaletteCommands(navigate)) registerPaletteCommand(cmd);
// The screen that REBINDS a chord. The keyboard rows teach an operator which chord runs what —
// the first half of a question whose second half ("that one's wrong, change it") had no palette
// row at all: Settings ▸ Keyboard was reachable only by opening Settings and finding the
// section. It carries `SHORTCUT_KEYWORDS`, the same tail as the rows it explains, so ONE
// `shortcuts` query returns the whole keyboard surface AND the way to rebind it. Neither flag-
// nor host-gated, so it resolves in a sister agent's window too.
_link(
  {
    id: "box:keybindings",
    label: "Settings: Keyboard",
    icon: glyph(Keyboard),
    keywords: ["settings", "rebind", "remap", "chord", "combo", ...SHORTCUT_KEYWORDS],
  },
  { kind: "global", section: "keybindings" },
);

/** Map a registered (core or fork) PaletteCommand onto a DS palette `Command`. The DS row
 *  has no shortcut slot, so a command that ADVERTISES a keybinding (ADR 0061 `keybinding` =
 *  a `registerKeybinding` id) renders its combo as the row's trailing hint — resolved
 *  through `effectiveCombo`, so the row shows the combo the operator REBOUND it to rather
 *  than a stale default. An explicit `hint` wins (a disabled row explains itself there). */
export function toDsCommand(pc: PaletteCommand): Command {
  const bound = pc.keybinding
    ? registeredKeybindings().find((b) => b.id === pc.keybinding)
    : undefined;
  return {
    id: pc.id,
    label: pc.label,
    group: pc.group ?? "Commands",
    keywords: pc.keywords ?? [],
    icon: pc.icon,
    hint: pc.hint ?? (bound ? formatCombo(effectiveCombo(bound)) : undefined),
    disabled: pc.disabled,
    run: (c) => pc.run({ close: () => c.close() }),
  };
}

/** The DS `CommandProvider` that serves `registerPaletteSource` rows — the seam's READ-TIME
 *  half (ADR 0061).
 *
 *  A source's rows exist to track live data (open chat tabs, a roster), and the DS's static
 *  path cannot carry them: `registerCommands` stores the array it is handed VERBATIM
 *  (`getStaticCommands: () => groups.flatMap(g => g.commands)`), so rows snapshotted in an
 *  effect freeze until something unrelated re-runs it. Nothing observes a source's data —
 *  `paletteCommandsVersion()` moves only on register/unregister — so a snapshot would go
 *  stale silently and stay stale: the fork's new tab missing from the palette, its closed tab
 *  still listed and still runnable. A provider is the read-time path the root view keeps: it
 *  re-invokes `getCommands(query)` on every palette READ — once when the palette opens (fired
 *  immediately, without the debounce, so a source's rows are on screen with the recents) and
 *  again on every keystroke (debounced 120ms) — which is exactly the promise the seam makes.
 *
 *  Statics stay on the snapshot path — a fixed list is correct to freeze, and it keeps them
 *  in their registered display position instead of trailing the list. `from: "dynamic"`
 *  already excludes any id a static claimed, so the two halves never double up.
 *
 *  The provider still applies `matchCommand` ITSELF: a provider is normally a remote search
 *  that already applied the query, so its results are appended verbatim and are NOT
 *  client-filtered by the view (that contract is inherited from the DS and preserved here).
 *  Skip this and a source's rows would ignore the search box entirely and sit under every
 *  query. `rank.ts` owns the matcher now, so the seam and the root view can never disagree
 *  about what "matches" means.
 *
 *  Everything is inside the try: a sync throw out of `getCommands` escapes into the view's
 *  `Promise.allSettled` callback as an unhandled rejection and leaves the palette spinning.
 *  `visiblePaletteCommands` contains a broken SOURCE already; this contains the mapping
 *  around it (a fork's exotic `icon`, a keybinding lookup). */
export function paletteSourceProvider(
  flagOn: (id: string) => boolean,
  onHost: boolean,
): CommandProvider {
  return {
    id: "ext-palette-sources",
    getCommands: (query) => {
      try {
        return visiblePaletteCommands(flagOn, onHost, "dynamic")
          .map(toDsCommand)
          .filter((c) => matchCommand(c, query));
      } catch {
        return [];
      }
    },
  };
}

/** Commands that are a DOOR, not a destination: running one only morphs the palette into
 *  another list, and that list records what you pick out of it (`withRecency`). Recording
 *  the door as well would spend one of the four recent slots on a row that is a permanent
 *  member of the Commands group anyway — evicting a surface the operator really did open,
 *  since a search-only surface has nowhere else on the empty list to appear.
 *
 *  Only `Open…` qualifies. `Ask <agent>` and `Fleet Room` morph too, but the view they morph
 *  into IS the destination — there is no second pick to record — so they stay recorded. */
const DOORWAY_COMMANDS = new Set(["open"]);

/** The root view's frecency write. Exported (and injected below) so the doorway rule is
 *  testable and lives next to the list it exists for, rather than inside the view. */
export function recordPaletteRun(c: Command): void {
  if (DOORWAY_COMMANDS.has(c.id)) return;
  markCommandUsed(c.id);
}

/** Wrap a command list so RUNNING one records its frecency.
 *
 *  The root view records every row IT renders, in one place, on purpose (`rootView.tsx`'s
 *  `run`). A SUBMORPH is a different view: `Open ▸` is a DS `commandsView`, and the DS's own
 *  `run` is `c.run(ctx)` with no hook of any kind. So the palette learned from typing and
 *  learned nothing from BROWSING — open Knowledge by typing its name and the recents list
 *  picks it up; open the same surface through `Open ▸`, which is where the guide sends you
 *  ("the built-in surfaces live one hop in"), and the only thing recorded was `Open…` itself.
 *
 *  Applied ONLY to the submorph's copy of the list, never to the search corpus the root view
 *  renders — that one already passes through the root's `run`, and wrapping both would count
 *  every typed run twice. */
export function withRecency(commands: Command[]): Command[] {
  return commands.map((c) => ({
    ...c,
    run: (ctx) => {
      markCommandUsed(c.id);
      c.run(ctx);
    },
  }));
}

/** Every built-in / fork surface as a "go to" command, resolved BY ID through ADR 0056's
 *  façade. The id list is the addressable handle; `viewFor` is the only thing that turns one
 *  back into a title/icon/kind — so a row's content always comes from the live registry
 *  rather than from a snapshot the palette took when the list was assembled. Plugin views
 *  are excluded: they already have root commands of their own (the "Plugins" group), and a
 *  second row per plugin view would just double every plugin in the search results. */
function resolveSurfaces(ids: string[], viewFor: (id: string) => View | undefined): View[] {
  return ids
    .map((id) => viewFor(id))
    .filter((v): v is View => !!v && v.kind !== "plugin");
}

/** Plugin-declared `commands:` (ADR 0057 §3) plus the window-level capabilities their
 *  compiled `run(ctx)` bodies need. Optional: a host that passes none (a test probe, a
 *  surface with no plugin presence) simply contributes no plugin command rows. */
export type PluginCommandsOptions = {
  /** Per-plugin contributions from runtime status — build with `pluginCommandSources`,
   *  the derivation the console App and the desktop launcher SHARE. */
  sources: PluginCommandSource[];
  /** Transient feedback for a `tool`/`emit` result — the window's `useToast()`. Passed in
   *  rather than called here because this hook is mounted in tests (and could be mounted
   *  in a fork surface) outside the DS `ToastProvider`, where `useToast()` throws. */
  notify: PluginCommandDeps["notify"];
};

/** An authenticated, same-origin JSON call for a compiled `tool`/`emit` action. Resolves
 *  on 2xx and rejects otherwise, so the row can toast the real outcome instead of claiming
 *  success on a 404. `apiUrl` slug-routes it to the focused fleet agent — the plugin whose
 *  route this is runs THERE — and the bearer is attached exactly as `lib/api`'s `request`
 *  does. The path itself was already asserted to sit under `/api/plugins/<id>/`
 *  (`pluginRoutePath`) before any row that reaches this line was created. */
async function pluginRequest(path: string, init: { method: string; body?: unknown }): Promise<void> {
  const token = authToken();
  const res = await fetch(apiUrl(path), {
    method: init.method,
    headers: {
      ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(init.body !== undefined ? { body: JSON.stringify(init.body) } : {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`.trim());
}

/** A DS palette registry whose ROOT VIEW IS OURS, seeded at CONSTRUCTION.
 *
 *  Construction, not an effect, and that is the load-bearing part: `CommandPalette`'s
 *  `viewMap` resolves during RENDER and synthesizes the DS's own `commandsView` for any root
 *  id nothing has claimed (command-palette.tsx:348-356), so a view registered from a
 *  `useEffect` would let the first commit render the DS default — unranked, no surfaces —
 *  and swap only on the next version bump. `createPaletteRegistry` copies `initial.views`
 *  synchronously (command-palette.tsx:117), so the view is in `getViews()` before the
 *  palette can ever read it.
 *
 *  The view is built BEFORE the registry it reads, hence the getter: the two are mutually
 *  referential, and a getter is the smaller price than a mutable field on the view. */
export function createRankedPaletteRegistry(
  config: Omit<RootViewConfig, "getRegistry"> = {},
): PaletteRegistry {
  let reg!: PaletteRegistry;
  const root = paletteRootView({ ...config, getRegistry: () => reg });
  reg = createPaletteRegistry({ views: [root] });
  return reg;
}

/** What only the HOST window can answer about itself.
 *
 *  `builtInChat` is "this window's chat slot is the BUILT-IN ChatSurface" — App computes it
 *  with `chatSlotProvider` (the same resolution `ChatSlot` renders with); the launcher leaves
 *  it false because it mounts no chat at all. It gates the chat's slash-command rows, which
 *  dispatch through a seam only the built-in surface publishes.
 *
 *  Deliberately a fact about the WINDOW, not about what is mounted: the DS AppShell unmounts
 *  a collapsed dock, so "is a chat slot registered right now?" flips every time the operator
 *  hides the panel — and gating rows on that emptied the Chat and Skills groups out of ⌘⇧K
 *  in exactly the state the palette is most useful in. */
export type PaletteHostOptions = {
  builtInChat?: boolean;
  /** Plugin-declared `commands:` (ADR 0057 §3). Folded into this one bag rather than a fifth
   *  positional parameter — two option objects on one hook is the shape that invites a caller
   *  to pass the wrong one. A host that passes none contributes no plugin command rows. */
  sources?: PluginCommandSource[];
  /** Transient feedback for a `tool`/`emit` result — the window's `useToast()`. Passed in
   *  rather than called here because this hook is mounted in tests (and could be mounted in a
   *  fork surface) outside the DS `ToastProvider`, where `useToast()` throws. */
  notify?: PluginCommandDeps["notify"];
};

/** Build the palette registry from the resolved view façade + the inline plugin views.
 *  Stable across renders; nav commands + inline views re-register only when their set
 *  changes (plugins enable/disable) — matching the DS registry's add/withdraw model. */
export function usePaletteRegistry(
  built: ViewsFacade,
  inlineViews: InlinePluginView[] = [],
  chat?: PaletteChatConfig,
  opts: PaletteHostOptions = {},
): PaletteRegistry {
  const { views, viewFor } = built;
  const inlineIds = useMemo(() => new Set(inlineViews.map((v) => v.id)), [inlineViews]);

  // Signatures key the re-register effects on the *content* (the array identity
  // changes every render; the ids/urls don't).
  const navSig = views.map((v) => `${v.id} ${v.title}`).join("|");
  const inlineSig = inlineViews.map((v) => `${v.id} ${v.url} ${v.title}`).join("|");

  // The search-only corpus. Held in a ref and read by the root view through a GETTER: the
  // view object is constructed once (see below) but the surface set changes as plugins load,
  // and a captured array would freeze the corpus at whatever was resolvable on first render.
  const surfaceCommands = useMemo(
    () =>
      resolveSurfaces(
        views.map((v) => v.id),
        viewFor,
      ).map<Command>((v) => ({
        id: `open:${v.id}`,
        label: v.title,
        icon: v.icon,
        // "Go to" is a real group header AND part of the DS haystack, so typing "go to"
        // still lists every surface the way the `Open` submorph does.
        group: "Go to",
        keywords: ["open", "go", "surface", v.kind],
        run: (c) => {
          navigate({ kind: "view", id: v.id });
          c.close();
        },
      })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [navSig],
  );
  const surfaceRef = useRef<Command[]>(surfaceCommands);
  surfaceRef.current = surfaceCommands;

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const registry = useMemo(
    () =>
      createRankedPaletteRegistry({
        searchOnly: () => surfaceRef.current,
        onRun: recordPaletteRun,
      }),
    [],
  );

  // Seam commands (ADR 0061) are RE-READ, not read once at mount, because their inputs land
  // late: `useFlagPredicate` fails closed until /api/flags answers (a flag-gated row would
  // otherwise stay hidden forever), and a fork module can register (or withdraw) after the
  // first render — which is what the registry's version counter reports. A keybinding
  // override re-labels the row that advertises it. Each feeds the effect below as a
  // dependency. The fourth input — a dynamic source's rows changing with the live data
  // behind them — deliberately does NOT: nothing observes that data, so no dependency could
  // track it. Those rows go through `paletteSourceProvider` instead, which the root view
  // re-invokes per read; the effect only decides WHETHER to wire it.
  const seamVersion = useSyncExternalStore(
    subscribePaletteCommands,
    paletteCommandsVersion,
    paletteCommandsVersion,
  );
  const flagOn = useFlagPredicate();
  const kbOverrides = useKeybindingOverrides((s) => s.overrides);

  // The live fleet roster (polled) → member names on the Fleet Room command's keywords. Works
  // in both the console window and the desktop launcher (both sit under QueryClientProvider).
  const { data: fleet } = useQuery(fleetQuery());
  const agents = fleet?.agents ?? [];
  const pluginViewsList = views.filter((v) => v.kind === "plugin");

  // Does this instance HAVE a knowledge store? The gate on the live-search provider below.
  // A CACHE READ, not a fetch: both mounts of this hook already sit under a component that
  // asks for this exact query key on boot (`App.tsx` for the console window, `Launcher.tsx`
  // for the frameless launcher), so react-query dedups it and the palette costs no request
  // of its own. Fails CLOSED — `undefined` until the status lands, then re-runs the effect —
  // for the same reason `useFlagPredicate` does: an unanswered capability is not a yes, and
  // a provider wired on a storeless instance is a "Searching…" spinner in front of a search
  // that can never return a row.
  const { data: runtime } = useQuery(runtimeStatusQuery());
  const knowledgeOn = runtime?.knowledge?.enabled === true;

  // ── The chat's own verbs (#3292), and what keeps them live ──────────────────────────
  // These rows go through the STATIC path (`registerCommands`), not `registerPaletteSource`,
  // for reasons about the PATH rather than about how live the data is: a provider's rows are
  // ORDERED but never RANKED against the corpus (`orderCommands` after `rankCommands` in
  // rootView), so `/clear` under the query "clear" would sit below every surface; declaring
  // `getCommands` at all costs a 120ms debounce and a spinner in every window that mounts the
  // palette, the chat-less launcher included; and a provider's results outlive the query they
  // answered (rootView stamps and drops them now, the DS's own view still doesn't —
  // protoContent#504). Statics are filtered and ranked against what is on screen,
  // synchronously.
  //
  // The cost of a snapshot is that something has to re-take it, so both live inputs are
  // subscriptions and both feed the effect below as dependencies:
  //   • the chat store, through `chatPaletteSignature()` — a STRING projection of everything
  //     a row renders from (the current session, whether it is the reusable blank, the two
  //     per-tab modes), so the store's per-streamed-token notifications don't re-register the
  //     whole group every frame;
  //   • `/api/chat/commands`, the same shared query the composer's `/` menu uses — so
  //     enabling a plugin or authoring a skill adds its row with no restart. Off in a window
  //     with no built-in chat (the launcher), which can't serve these rows anyway.
  const chatSig = useSyncExternalStore(
    chatStore.subscribe,
    chatPaletteSignature,
    chatPaletteSignature,
  );
  const builtInChat = !!opts.builtInChat;
  const { data: chatCommands } = useQuery({ ...chatCommandsQuery(), enabled: builtInChat });
  const skills = useMemo(
    () => (chatCommands?.commands ?? []).filter((c) => c.kind === "skill"),
    [chatCommands],
  );
  const skillSig = skills.map((c) => `${c.name} ${c.description}`).join("|");

  // Re-register the fleet section only when the roster's identity/status/name actually changes
  // (React Query's structural sharing keeps `agents` stable when the 3s poll returns equal data).
  const fleetSig = agents.map((a) => `${a.host ? "host" : a.id}:${a.running}:${a.name}`).join("|");
  // Plugin-declared commands (ADR 0057 §3) — keyed on the CONTENT the compile step reads,
  // so the rows re-register when a plugin is enabled/disabled, finishes loading, is renamed
  // or edits its manifest, but not on every status poll (the array identity churns each
  // time; this string doesn't). EVERY compile input is in it, not just the visible fields:
  // a route or topic edit rewrites what the row fires, `loaded` flips a `tool` row between
  // runnable and disabled, the name is the attribution chip, and the view ids decide
  // whether a `navigate` compiles at all. The icon resolver is the one input left out —
  // it is a per-window constant.
  const cmdSources = opts.sources ?? [];
  const cmdSig = cmdSources
    .map((p) => JSON.stringify([p.id, p.name, p.loaded, [...p.viewIds], p.commands]))
    .join("|");

  // Views the palette can morph into: inline plugin iframes, the chat view, and the
  // `Open` submorph — the same surface list the search corpus uses, so browsing and
  // searching can never disagree about what exists.
  useEffect(() => {
    const vs: PaletteView[] = inlineViews.map((v) =>
      pluginView({
        id: v.id,
        title: v.title,
        url: v.url,
        theme: v.theme,
        token: v.token,
        sandbox: v.sandbox,
        height: 460,
      }),
    );
    if (chat) vs.push(chat.view);
    // The palette Fleet Room morph-view (sibling of the chat view). Opening a member routes
    // through the shared nav chokepoint so it forwards from the launcher window too.
    vs.push(
      fleetRoomView({
        onOpenAgent: (slug) => {
          markAgentOpened(slug); // legacy fleet store (fleetPalette.ts) — kept exactly as-is
          markAgentUsed(slug); // namespaced palette store (agent:<slug>)
          navigate({ kind: "agent", slug });
        },
      }),
    );
    vs.push(memberDmView()); // Fleet Room → DM a member (the wired chat, retargeted)
    vs.push({
      // `withRecency` because this list is rendered by the DS, not by our root view — the
      // one `run()` chokepoint does not reach in here. See the helper.
      ...commandsView({
        commands: withRecency(surfaceRef.current),
        placeholder: "Open a surface…",
      }),
      id: "open",
      title: "Open",
    });
    return registry.registerViews(vs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navSig, inlineSig, chat, registry]);

  // Root commands, registered in DISPLAY order (the empty-query list renders them in
  // registration order, after recents). Agents → Plugins → Commands. Re-registered
  // atomically when the view set changes (plugins load), so the order never drifts.
  useEffect(() => {
    const offChat = chat
      ? registry.registerCommands([
          {
            // "Chat with <agent>" collided with the Chat SURFACE the moment surfaces became
            // searchable: two rows matching "chat", one of them the transient palette chat
            // and one the real surface. Relabelled + re-id'd HERE, in the adapter — the chat
            // morph-VIEW's id/title are declared twice (App.tsx and Launcher.tsx) and
            // touching those literals would need a two-file change to stay in sync.
            id: "chat:ask",
            label: `Ask ${chat.name}`,
            hint: "quick chat",
            icon: chat.icon,
            group: "Agents",
            keywords: ["chat", "ask", "talk", "agent", "quick", "question"],
            run: (c) => c.enter("chat"),
          },
        ])
      : undefined;
    // Fleet Room — the co-present roster + address/broadcast, opened as a morph-view. Top of
    // the Agents group, right under "Ask <this agent>". Quick-chat (#1733) and Toggle Fleet
    // Agent (#1769) are FOLDED INTO the room: the roster row carries DM / open-console /
    // start / stop, and every member's name rides this command's keywords — so typing "ava"
    // still lands you one step from her. (Re-registered on fleetSig, so the keyword list
    // tracks the live roster. Those keywords are the ONLY reason a member's name finds this
    // row, which is why ranking must never drop a keyword-only match — see rank.ts.)
    // Open in every sister agent's window, mirroring the Fleet settings gate: `/api/fleet` is
    // a HUB path (never slug-scoped), so a member window's room shows the hub's real roster
    // and its DM/broadcast targets are the real siblings. Only a member reached DIRECTLY on
    // its own port sees a fleet-of-one — there the command disables with a pointer at the
    // host rather than hiding, so it stays discoverable and explains itself.
    const fleetGate = fleetSettingsDisabledReason(agents);
    const offFleetRoom = registry.registerCommands([
      {
        id: "fleet-room",
        label: "Fleet Room",
        icon: glyph(Users),
        hint: fleetGate ? "host instance only" : "members · DM · broadcast",
        disabled: !!fleetGate,
        group: "Agents",
        keywords: [
          "fleet",
          "room",
          "members",
          "agents",
          "team",
          "crew",
          "broadcast",
          "dm",
          "roster",
          "chat",
          "switch",
          "toggle",
          "start",
          "stop",
          ...agents.map((a) => a.name),
        ],
        run: (c) => c.enter("fleet-room"),
      },
    ]);
    // Each plugin's views: inline ones morph IN PLACE (also in the launcher window); a
    // rail view navigates — routed through `navigate()` so the launcher hands it off to
    // the main window instead of mutating its own (shell-less) store.
    const pluginCommands: Command[] = pluginViewsList.map((v) => {
      const inline = inlineIds.has(v.id);
      return {
        id: `nav:${v.id}`,
        label: v.title,
        // No "open" verb/keyword here — `Open` is its own command and would collide.
        // An inline plugin morphs in place (no hint); a rail view navigates ("go to").
        hint: inline ? undefined : "go to",
        icon: v.icon,
        group: "Plugins",
        keywords: ["plugin", v.kind],
        run: inline
          ? (c) => c.enter(v.id)
          : (c) => {
              navigate({ kind: "view", id: v.id });
              c.close();
            },
      };
    });
    const offPlugins = pluginCommands.length ? registry.registerCommands(pluginCommands) : undefined;
    // Each enabled plugin's DECLARED commands (ADR 0057 §3/§4), compiled by the trusted
    // adapter — the only place manifest data becomes behavior. Registered right after the
    // plugin VIEW rows above and split per (section, plugin) by `pluginCommandGroups`, for
    // two reasons: the DS stamps `source` per registration (so every row carries its own
    // plugin's attribution chip), and the root view opens a group heading only when the
    // group CHANGES, so a default-grouped row landing next to the "Plugins" nav rows
    // continues that section instead of opening a second one under the same name.
    const offDeclared = pluginCommandGroups(cmdSources, {
      inlineViewIds: inlineIds,
      navigate,
      request: pluginRequest,
      notify: opts.notify ?? (() => {}),
    }).map((g) => registry.registerCommands(g.commands, { source: g.source }));
    // Commands group: `Open` (morphs to the built-in surfaces) and the deep-link actions.
    // (Fleet start/stop lives on the Fleet Room roster now — #1769 folded in.)
    const openCommand: Command = {
      id: "open",
      label: "Open…",
      hint: "surface",
      icon: glyph(PanelsTopLeft),
      group: "Commands",
      keywords: ["open", "go to", "surface", "view", "navigate", "switch", "panel"],
      run: (c) => c.enter("open"),
    };
    const offCommands = registry.registerCommands([
      openCommand,
      // The GATED read: a `flag`-off or (off-host) `hostOnly` command is omitted, exactly as
      // a gated Settings section is (`visibleSections`). Gating here rather than at
      // registration is what lets a late `/api/flags` answer reveal the row.
      // STATICS ONLY — a fixed list is safe to snapshot. Source rows would freeze here; they
      // take the read-time provider path below instead.
      ...visiblePaletteCommands(flagOn, isHostConsole(), "static").map(toDsCommand),
    ]);
    // The chat's own verbs — the client slash commands and the server's user-facing skills
    // (#3292). Registered LAST so the Chat and Skills groups sit after the fixed ones in the
    // untyped list, and registered STATICALLY so a row is client-filtered against the query
    // on screen (see the note where `chatSig` is read). Flag-gated here rather than in the
    // row builder, exactly as the seam's statics are: a row gated on a flag still in flight
    // (`/publish`) appears when `/api/flags` lands instead of being hidden for the life of
    // the window.
    const chatRows = chatSlashPaletteRows(navigate, { reachable: builtInChat, skills });
    const offChatRows = chatRows.length
      ? registry.registerCommands(chatRows.filter((c) => !c.flag || flagOn(c.flag)).map(toDsCommand))
      : undefined;
    // Dynamic sources, served per palette read. Wired only when a fork registered one: the
    // root view shows its "Searching…" spinner whenever ANY provider declares
    // `getCommands`, and core ships zero sources — so an unconditional provider would put a
    // 120ms spinner in front of every keystroke in the default console. Registering a source
    // bumps `seamVersion`, which re-runs this effect and wires the provider then.
    const offSources = hasPaletteSources()
      ? registry.registerProvider(paletteSourceProvider(flagOn, isHostConsole()))
      : undefined;
    // Live knowledge search — the console's first REMOTE provider (the source provider above
    // is a synchronous read of in-process registrations). See the module header of
    // `./knowledgeSearch.ts` for why the row cap, the failure row and the empty-query browse
    // guard live where they do, and `rootView.tsx` for the debounce and cancellation this
    // provider therefore does NOT own. `navigate` is handed in so the launcher window's
    // forwarding sink is the same chokepoint here as for every other palette navigation.
    //
    // Registered LAST, and in THIS effect rather than one of its own, so provider order is
    // deterministic. Under the host-owned root, provider order is the STABLE TIEBREAK inside
    // a match tier (`orderCommands` sorts tier → frecency → the row's index in the flattened
    // provider read, which is registration order) — so a fork's dynamic row and a knowledge
    // row of equal tier come out in a fixed order rather than one that flips whenever an
    // unrelated effect re-runs. A separate effect registers once and then never moves, so
    // the first re-run of THIS one would re-append the source provider behind it and quietly
    // invert that pair. Riding along costs nothing: this effect already bumps the registry
    // version on each of its deps, which is what re-fires the open palette's read.
    // (The older reason — a second **Commands** header printing under the **Knowledge** one
    // — is gone: the host root drops group headers entirely on the typed path, because
    // contiguity only means "grouping" while the list is in registration order.)
    //
    // GATED, on the same rule the source provider is gated on and for the same reason: a
    // provider that declares `getCommands` is what raises the root view's "Searching…"
    // affordance (`rootView.tsx` early-returns only when NO provider has one), so a provider
    // registered where it can never return a row is a spinner in front of a search that does
    // not exist. An instance with `knowledge.enabled: false` has no store at all — every
    // query would 200 with `{enabled: false, results: []}` — so the honest number of
    // providers there is zero, exactly as `hasPaletteSources()` keeps it zero for sources.
    // The capability is a server answer, but reading it is FREE here: `/api/runtime/status`
    // is already fetched on boot by both hosts that mount this hook, so `knowledgeOn` is a
    // react-query cache read and the earlier objection to gating (a silent extra request per
    // boot) does not hold. It fails closed and re-runs this effect when the status lands.
    //
    // What the gate does NOT reach: on an instance that DOES have a store, a TYPED query
    // shorter than `KNOWLEDGE_MIN_QUERY` still raises the spinner for the 120ms debounce,
    // because the root cannot know a provider will decline the query until it has asked.
    // The empty root no longer does — `rootView.tsx` skips both the debounce and the
    // spinner on an untyped query, which is the half of this that #3289 fixed at the root.
    const offKnowledge = knowledgeOn
      ? registry.registerProvider(knowledgeSearchProvider(navigate))
      : undefined;
    return () => {
      offChat?.();
      offFleetRoom();
      offPlugins?.();
      offDeclared.forEach((off) => off());
      offCommands();
      offChatRows?.();
      offSources?.();
      offKnowledge?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    navSig,
    inlineSig,
    fleetSig,
    cmdSig,
    chat,
    registry,
    seamVersion,
    flagOn,
    kbOverrides,
    builtInChat,
    chatSig,
    skillSig,
    knowledgeOn,
  ]);

  return registry;
}
