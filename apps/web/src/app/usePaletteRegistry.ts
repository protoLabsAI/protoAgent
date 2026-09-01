// ADR 0057 — the command-palette adapter. Feeds the DS palette registry from the
// console's existing sources, organized to be command-driven rather than a flat list of
// places. The root view reads top-to-bottom as: Agents (chat) → Plugins (each plugin's
// views) → Commands. The built-in surfaces are NOT dumped at the root — an `Open…` command
// morphs into an `Open ▸` submorph (a self-contained command list) so you don't see every
// surface until you ask for one. Deep-link actions ride in the Commands group too.
//
// Inline plugin views (a plugin view that opts in via `views[].palette: "inline"`) are
// registered as DS `pluginView()`s — their command morphs the palette body into the
// plugin's own iframe (themed/authed via the handshake) instead of navigating to its rail.
import type { ReactNode } from "react";
import { useEffect, useMemo, useSyncExternalStore } from "react";
import { commandsView, createPaletteRegistry, pluginView } from "@protolabsai/ui/command-palette";
import type {
  Command,
  CommandProvider,
  PaletteRegistry,
  PaletteView,
} from "@protolabsai/ui/command-palette";
import { useUI } from "../state/uiStore";
import type { View } from "../lib/viewRegistry";
import {
  hasPaletteSources,
  paletteCommandsVersion,
  registerPaletteCommand,
  subscribePaletteCommands,
  visiblePaletteCommands,
} from "../ext/paletteRegistry";
import type { PaletteCommand } from "../ext/paletteRegistry";
import { registeredKeybindings } from "../ext/keybindingRegistry";
import { formatCombo } from "../keybindings/combo";
import { effectiveCombo, useKeybindingOverrides } from "../keybindings/overrides";
import { useFlagPredicate } from "../flags/flags";
import { useQuery } from "@tanstack/react-query";
import { agentHref, isHostConsole } from "../lib/api";
import { chatCommandsQuery, fleetQuery } from "../lib/queries";
import { chatStore } from "../chat/chat-store";
import { chatPaletteSignature, chatSlashPaletteRows } from "./chatSlashPalette";
import { markAgentOpened } from "./fleetPalette";
import { fleetRoomView } from "./FleetRoom";
import { fleetSettingsDisabledReason } from "./fleetSettingsGate";
import { memberDmView } from "./PaletteChat";

/** Optional inline chat with the focused agent (ADR 0057). App builds the native chat
 *  PaletteView (it needs JSX + the focused agent name); the adapter registers it + a
 *  "Chat with <agent>" command that morphs into it. */
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

/** Open any view by id, routed to the dock it actually lives on (and uncollapsed).
 *  Reads live state via the store's `getState()` so it isn't a render subscription.
 *  A HIDDEN surface (railOrder.hidden — enabled but not shown) is un-hidden first: the
 *  palette is the restore point, so ⌘⇧K → a hidden view's name brings it back onto a dock. */
export function openView(id: string) {
  const ui = useUI.getState();
  if ((ui.railOrder.hidden ?? []).includes(id)) ui.showSurface(id); // restore onto its dock, then route
  const ro = useUI.getState().railOrder; // re-read: showSurface mutated it
  // The mobile shell reads `mobileActive`, NOT the per-dock ids — so without this every
  // programmatic navigation (a palette "go to", the rail context menu, a plugin's `ui.navigate`,
  // a launcher intent) silently did nothing on a phone: it moved a dock the mobile shell
  // never renders. Set both; `mobileActive` is inert on desktop.
  ui.setMobileActive(id);
  if (ro.right.includes(id)) {
    ui.setRightCollapsed(false);
    ui.setRightPanel(id);
  } else if (ro.bottom.includes(id)) {
    ui.setBottomCollapsed(false);
    ui.setBottomPanel(id);
  } else {
    ui.setLeftCollapsed(false);
    ui.setSurface(id);
  }
}

// ── Navigation handoff (desktop launcher, ADR 0057) ────────────────────────────────
// Every palette navigation funnels through `navigate(intent)` so it has ONE chokepoint.
// In the normal console window the intent applies to THIS window's store (the default).
// In the frameless desktop launcher window the store is a separate JS context with no
// shell — so the launcher swaps the sink (`setPaletteNavigator`) to forward the intent
// to the main window over a Tauri event, which replays it there via `applyNavIntent`.

/** A serializable description of "where the palette wants to go" — so it can cross the
 *  window boundary as a plain event payload. */
export type NavIntent =
  | { kind: "view"; id: string }
  | { kind: "plugins"; tab: "local" | "market" }
  | { kind: "global"; section?: string }
  | { kind: "agent"; slug: string };

/** Apply an intent to THIS window's UI store. The default navigator, and what the main
 *  window calls when it receives a forwarded intent from the launcher. */
export function applyNavIntent(intent: NavIntent) {
  const ui = useUI.getState();
  switch (intent.kind) {
    case "view":
      openView(intent.id);
      break;
    case "plugins":
      // Plugins is a Settings section; Settings is the dialog now (2026-06). Open it on the
      // Plugins section with the right inner tab (Installed/Discover).
      ui.setPluginsTab(intent.tab);
      ui.openGlobalSettings("plugins");
      break;
    case "global":
      ui.openGlobalSettings(intent.section);
      break;
    case "agent":
      // Switch the console to another fleet agent (slug-routed, ADR 0042) — a full navigation,
      // since that agent's chat, threads, and API surface all key off the URL slug. This runs in
      // a real console window (the launcher forwards the intent to the main window), so
      // `window.location` targets the window the operator is actually looking at.
      window.location.href = agentHref(intent.slug);
      break;
  }
}

let navigator: (intent: NavIntent) => void = applyNavIntent;

/** Override where palette navigation goes (the launcher forwards to the main window).
 *  Pass `null` to restore the default local apply. */
export function setPaletteNavigator(fn: ((intent: NavIntent) => void) | null) {
  navigator = fn ?? applyNavIntent;
}

/** The single entry point every nav command + deep-link runs through. */
function navigate(intent: NavIntent) {
  navigator(intent);
}

// Core deep-link palette commands — DOGFOODED through the public `registerPaletteCommand`
// seam (ADR 0061), so core uses the same path a fork does (no bypass). Each deep-link is a
// serializable NavIntent routed through `navigate()`, so it works identically in the console
// window (apply locally) and the desktop launcher (forward to the main window). The sub-tab
// ids are the uiStore union types (source of truth), so they can't drift into a 404.
// (Inbox moved to a utility-bar widget; Schedule is a top-level rail surface that
// auto-registers as a "go to" nav command — so no Activity deep-links here.)
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
_link("plug:market", "Plugins: Discover", ["plugins", "discover", "market", "directory", "browse"], {
  kind: "plugins",
  tab: "market",
});
// Install-from-URL is the advanced action under Installed now (ADR 0059 D4) — land there.
_link("plug:download", "Plugins: Install from URL", ["plugins", "install", "url", "git"], {
  kind: "plugins",
  tab: "local",
});
// Settings is the consolidated dialog now (2026-06) — opened from the utility-bar pill,
// the drawer, or these palette commands. A bare "Settings" command + Box-section deep-links.
_link("settings", "Settings", ["settings", "config", "preferences", "options"], { kind: "global" });
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
function toDsCommand(pc: PaletteCommand): Command {
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

/** Does this row match what the operator typed? A deliberate mirror of the DS commands
 *  view's own `matchCommand` (module-private in @protolabsai/ui): every whitespace-separated
 *  term must appear somewhere in the row's label / hint / group / keywords, case-insensitively.
 *  (The DS also searches a row's `source` chip label; the seam stamps none, so there is
 *  nothing to search there.)
 *
 *  The seam's provider has to apply it ITSELF because the DS client-filters only its STATIC
 *  commands — a provider is normally a remote search that already applied the query, so its
 *  results are appended verbatim (`command-palette.views.tsx`: `[...baseCommands.filter(…),
 *  ...dynamic]`). Skip this and a source's rows would ignore the search box entirely and sit
 *  under every query. Keep it in step with the DS if that matcher changes; the seam's own
 *  tests pin the behavior it must have (all terms, any field, case-insensitive). */
function matchesQuery(c: Command, q: string): boolean {
  const query = q.trim().toLowerCase();
  if (!query) return true;
  const hay = [c.label, c.hint, c.group, ...(c.keywords ?? [])]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return query.split(/\s+/).every((term) => hay.includes(term));
}

/** The DS `CommandProvider` that serves `registerPaletteSource` rows — the seam's READ-TIME
 *  half (ADR 0061).
 *
 *  A source's rows exist to track live data (open chat tabs, a roster), and the DS's static
 *  path cannot carry them: `registerCommands` stores the array it is handed VERBATIM
 *  (`getStaticCommands: () => groups.flatMap(g => g.commands)`), so rows snapshotted in an
 *  effect freeze until something unrelated re-runs it. Nothing observes a source's data —
 *  `paletteCommandsVersion()` moves only on register/unregister — so a snapshot would go
 *  stale silently and stay stale: the fork's new tab missing from ⌘K, its closed tab still
 *  listed and still runnable. A provider is the DS's actual read-time path: the commands
 *  view re-invokes `getCommands(query)` every time the palette opens and on every keystroke
 *  (debounced 120ms), which is exactly the promise the seam makes.
 *
 *  Statics stay on the snapshot path — a fixed list is correct to freeze, and it keeps them
 *  in their registered display position instead of trailing the list. `from: "dynamic"`
 *  already excludes any id a static claimed, so the two halves never double up.
 *
 *  Everything is inside the try: a sync throw out of `getCommands` escapes into the DS's
 *  `Promise.allSettled` callback as an unhandled rejection and leaves the palette spinning
 *  "Searching…" forever. `visiblePaletteCommands` contains a broken SOURCE already; this
 *  contains the mapping around it (a fork's exotic `icon`, a keybinding lookup). */
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
          .filter((c) => matchesQuery(c, query));
      } catch {
        return [];
      }
    },
  };
}

/** What only the HOST window can answer about itself.
 *
 *  `builtInChat` is "this window's chat slot is the BUILT-IN ChatSurface" — App computes it
 *  with `chatSlotProvider` (the same resolution `ChatSlot` renders with), the launcher leaves
 *  it false because it mounts no chat at all. It gates the chat's slash-command rows, which
 *  dispatch through a seam only the built-in surface publishes.
 *
 *  Deliberately a fact about the WINDOW, not about what is mounted: the DS AppShell unmounts
 *  a collapsed dock, so "is a chat slot registered right now?" flips every time the operator
 *  hides the panel — and gating rows on that emptied the Chat and Skills groups out of ⌘⇧K
 *  in exactly the state the palette is most useful in. */
export type PaletteHostOptions = {
  builtInChat?: boolean;
};

/** Build the palette registry from the resolved view list + the inline plugin views.
 *  Stable across renders; nav commands + inline views re-register only when their set
 *  changes (plugins enable/disable) — matching the DS registry's add/withdraw model. */
export function usePaletteRegistry(
  views: View[],
  inlineViews: InlinePluginView[] = [],
  chat?: PaletteChatConfig,
  opts: PaletteHostOptions = {},
): PaletteRegistry {
  const registry = useMemo(() => createPaletteRegistry(), []);
  const inlineIds = useMemo(() => new Set(inlineViews.map((v) => v.id)), [inlineViews]);

  // Seam commands (ADR 0061) are RE-READ, not read once at mount, because their inputs land
  // late: `useFlagPredicate` fails closed until /api/flags answers (a flag-gated row would
  // otherwise stay hidden forever), and a fork module can register (or withdraw) after the
  // first render — which is what the registry's version counter reports. A keybinding
  // override re-labels the row that advertises it. Each feeds the effect below as a
  // dependency. The fourth input — a dynamic source's rows changing with the live data
  // behind them — deliberately does NOT: nothing observes that data, so no dependency could
  // track it. Those rows go through `paletteSourceProvider` instead, which the DS palette
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

  // ── The chat's own verbs (#3292), and what keeps them live ──────────────────────────
  // These rows go through the STATIC path (`registerCommands`), not `registerPaletteSource`:
  // the DS serves a source through a debounced provider that never clears the PREVIOUS
  // query's results, so a stale row stays listed — and runnable by Enter — for 120ms after
  // every keystroke. Fine for a fork's read-only "open tab X" list; not fine for the chat's
  // ACTION rows. Statics are client-filtered synchronously against what is on screen.
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

  // Built-in surfaces (core + fork/ext) live behind `Open ▸`; plugin views are their own
  // root section. A `session` view (none today) would ride with the built-ins.
  const surfaceViews = views.filter((v) => v.kind !== "plugin");
  const pluginViewsList = views.filter((v) => v.kind === "plugin");

  // Signatures key the re-register effects on the *content* (the array identity
  // changes every render; the ids/urls don't).
  const navSig = views.map((v) => `${v.id} ${v.title}`).join("|");
  const inlineSig = inlineViews.map((v) => `${v.id} ${v.url} ${v.title}`).join("|");
  // Re-register the fleet section only when the roster's identity/status/name actually changes
  // (React Query's structural sharing keeps `agents` stable when the 3s poll returns equal data).
  const fleetSig = agents.map((a) => `${a.host ? "host" : a.id}:${a.running}:${a.name}`).join("|");

  // Views the palette can morph into: inline plugin iframes, the chat view, and the
  // `Open ▸` submorph (a self-contained command list of the built-in surfaces, so the root
  // stays a short command list — you don't see every surface until you enter Open).
  useEffect(() => {
    const openSurfaceCommands: Command[] = surfaceViews.map((v) => ({
      id: `open:${v.id}`,
      label: v.title,
      icon: v.icon,
      keywords: ["open", "go", "surface", v.kind],
      run: (c) => {
        navigate({ kind: "view", id: v.id });
        c.close();
      },
    }));
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
    // The Fleet Room morph-view (sibling of the chat view). Opening a member routes
    // through the shared nav chokepoint so it forwards from the launcher window too.
    vs.push(
      fleetRoomView({
        onOpenAgent: (slug) => {
          markAgentOpened(slug);
          navigate({ kind: "agent", slug });
        },
      }),
    );
    vs.push(memberDmView()); // Fleet Room → DM a member (the wired chat, retargeted)
    vs.push({
      ...commandsView({ commands: openSurfaceCommands, placeholder: "Open a surface…" }),
      id: "open",
      title: "Open",
    });
    return registry.registerViews(vs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navSig, inlineSig, chat, registry]);

  // Root commands, registered in DISPLAY order (the list renders groups in registration
  // order). Agents → Plugins → Commands. Re-registered atomically when the view set
  // changes (plugins load), so the order never drifts.
  useEffect(() => {
    const offChat = chat
      ? registry.registerCommands([
          {
            id: "chat",
            label: `Chat with ${chat.name}`,
            hint: "ask the agent",
            icon: chat.icon,
            group: "Agents",
            keywords: ["chat", "ask", "talk", "agent"],
            run: (c) => c.enter("chat"),
          },
        ])
      : undefined;
    // Fleet Room (the palette-UX overhaul) — the co-present roster + address/broadcast, opened
    // as a morph-view. Top of the Agents group, right under "Chat with <this agent>".
    // Quick-chat (#1733) and Toggle Fleet Agent (#1769) are FOLDED INTO the room: the
    // roster row carries DM / open-console / start / stop, and every member's name rides
    // this command's keywords — so typing "ava" still lands you one step from her.
    // (Re-registered on fleetSig, so the keyword list tracks the live roster.)
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
        // No "open" verb/keyword here — `Open…` is its own command now and would collide.
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
    // Commands group: `Open ▸` (morphs to the built-in surfaces) and the deep-link actions.
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
    // The chat's own verbs — the client slash commands and the server's user-facing skills
    // (#3292). Registered LAST so the Chat and Skills groups render after the fixed ones, and
    // registered STATICALLY so the DS client-filters them synchronously against the live
    // query (see the note where `chatSig` is read). Flag-gated here rather than in the row
    // builder, exactly as the seam's statics are: a row gated on a flag still in flight
    // (`/publish`) appears when `/api/flags` lands instead of being hidden for the life of
    // the window.
    const chatRows = chatSlashPaletteRows(navigate, { reachable: builtInChat, skills });
    const offChatRows = chatRows.length
      ? registry.registerCommands(chatRows.filter((c) => !c.flag || flagOn(c.flag)).map(toDsCommand))
      : undefined;
    // Dynamic sources, served per palette read. Wired only when a source exists: the DS shows
    // its "Searching…" spinner (and debounces 120ms) whenever ANY provider declares
    // `getCommands`, so an unconditional provider would charge every keystroke for nothing —
    // in EVERY window that mounts this hook, the desktop launcher included. Core ships no
    // source (#3292 nearly did; its rows are statics above, for the staleness reason spelled
    // out there), so in the default console this stays unwired and keystrokes stay instant.
    // Registering one bumps `seamVersion`, which re-runs this effect and wires the provider
    // then.
    const offSources = hasPaletteSources()
      ? registry.registerProvider(paletteSourceProvider(flagOn, isHostConsole()))
      : undefined;
    return () => {
      offChat?.();
      offFleetRoom();
      offPlugins?.();
      offCommands();
      offChatRows?.();
      offSources?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    navSig,
    inlineSig,
    fleetSig,
    chat,
    registry,
    seamVersion,
    flagOn,
    kbOverrides,
    builtInChat,
    chatSig,
    skillSig,
  ]);

  return registry;
}
