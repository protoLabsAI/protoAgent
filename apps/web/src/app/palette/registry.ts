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
import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";
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
import { isHostConsole } from "../../lib/api";
import { fleetQuery } from "../../lib/queries";
import { markAgentOpened } from "../fleetPalette";
import { fleetRoomView } from "../FleetRoom";
import { fleetSettingsDisabledReason } from "../fleetSettingsGate";
import { memberDmView } from "../PaletteChat";
import { navigate } from "./nav";
import type { NavIntent } from "./nav";
import { matchCommand } from "./rank";
import { markAgentUsed } from "./recents";
import { paletteRootView } from "./rootView";
import type { RootViewConfig } from "./rootView";

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
const _link = (id: string, label: string, keywords: string[], intent: NavIntent) =>
  registerPaletteCommand({
    id,
    label,
    group: "Commands",
    keywords,
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
// Settings is the consolidated dialog now (2026-06) — opened from the utility-bar pill,
// the drawer, or these palette commands. A bare "Settings" command + Box-section deep-links.
_link("settings", "Settings", ["settings", "config", "preferences", "options"], { kind: "global" });
_link("plug:market", "Plugins: Discover", ["plugins", "discover", "market", "directory", "browse"], {
  kind: "plugins",
  tab: "market",
});
// Install-from-URL is the advanced action under Installed now (ADR 0059 D4) — land there.
_link("plug:download", "Plugins: Install from URL", ["plugins", "install", "url", "git"], {
  kind: "plugins",
  tab: "local",
});
_link("box:fleet", "Settings: Fleet", ["fleet", "agents", "box"], { kind: "global", section: "fleet" });
_link("box:telemetry", "Settings: Telemetry", ["telemetry", "metrics", "box", "global"], {
  kind: "global",
  section: "telemetry",
});

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
 *  re-invokes `getCommands(query)` on every keystroke (debounced 120ms), which is exactly the
 *  promise the seam makes.
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

/** Build the palette registry from the resolved view façade + the inline plugin views.
 *  Stable across renders; nav commands + inline views re-register only when their set
 *  changes (plugins enable/disable) — matching the DS registry's add/withdraw model. */
export function usePaletteRegistry(
  built: ViewsFacade,
  inlineViews: InlinePluginView[] = [],
  chat?: PaletteChatConfig,
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
    () => createRankedPaletteRegistry({ searchOnly: () => surfaceRef.current }),
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

  // Re-register the fleet section only when the roster's identity/status/name actually changes
  // (React Query's structural sharing keeps `agents` stable when the 3s poll returns equal data).
  const fleetSig = agents.map((a) => `${a.host ? "host" : a.id}:${a.running}:${a.name}`).join("|");

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
      ...commandsView({ commands: surfaceRef.current, placeholder: "Open a surface…" }),
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
    // Commands group: `Open` (morphs to the built-in surfaces) and the deep-link actions.
    // (Fleet start/stop lives on the Fleet Room roster now — #1769 folded in.)
    const openCommand: Command = {
      id: "open",
      label: "Open…",
      hint: "surface",
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
    // Dynamic sources, served per palette read. Wired only when a fork registered one: the
    // root view shows its "Searching…" spinner whenever ANY provider declares
    // `getCommands`, and core ships zero sources — so an unconditional provider would put a
    // 120ms spinner in front of every keystroke in the default console. Registering a source
    // bumps `seamVersion`, which re-runs this effect and wires the provider then.
    const offSources = hasPaletteSources()
      ? registry.registerProvider(paletteSourceProvider(flagOn, isHostConsole()))
      : undefined;
    return () => {
      offChat?.();
      offFleetRoom();
      offPlugins?.();
      offCommands();
      offSources?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navSig, inlineSig, fleetSig, chat, registry, seamVersion, flagOn, kbOverrides]);

  return registry;
}
