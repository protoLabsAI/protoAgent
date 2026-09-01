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
import { flushSync } from "react-dom";
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
import { runBindingById } from "../keybindings/useKeybindings";
import { registerKeybindingCommands, SHORTCUT_KEYWORDS } from "./keybindingCommands";
import { useFlagPredicate } from "../flags/flags";
import { useQuery } from "@tanstack/react-query";
import { agentHref, isHostConsole } from "../lib/api";
import { fleetQuery } from "../lib/queries";
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
  | { kind: "agent"; slug: string }
  // Run a registered keybinding's action (ADR 0063) — what a ⌘K row that advertises a
  // shortcut does. An INTENT rather than a direct `binding.run()` at the row for the usual
  // launcher reason: these actions mutate the console's stores and walk its DOM, both of
  // which are absent in the frameless launcher window, so the work has to be able to cross
  // to the main window as a plain payload. `surface` is the view that makes a SCOPED
  // binding's scope real — see the case in `applyNavIntent`.
  | { kind: "keybinding"; id: string; surface?: string };

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
    case "keybinding": {
      // Make the binding's SCOPE real before running it, rather than bypassing it.
      // `resolveBinding` is the only place `scope` is enforced and a palette row calls
      // `run()` directly, so a chat-scoped action would otherwise fire from an overlay that
      // is never inside `[data-kb-scope="chat"]`. Navigating first is also what the operator
      // asked for — "Clear conversation" chosen from Knowledge means "go to chat and clear
      // it". A row for a scope with no surface is never built (keybindingCommands.ts).
      //
      // …and `flushSync`, because "navigating first" has to mean the DOM, not the store.
      // `openView` only writes zustand state; the row runs from a React event handler, so
      // React commits AFTER the handler returns — the next line would otherwise read a DOM
      // the navigation had not produced yet. That matters because the chat slot's #613
      // "mounted for the app's LIFETIME" contract is about the slot within its DOCK, and the
      // DS AppShell renders each column conditionally (`const showLeft = !leftCollapsed`;
      // `{showLeft && <main className="pl-appshell__col--left">…</main>}`, app-shell.tsx). A
      // collapsed dock takes `.chat-session-slot` and `[data-kb-scope="chat"]` out of the
      // document entirely, so `chat.tool.toggle` — the one action here that WALKS the DOM
      // (toolCollapse.ts `chatRoot()`) — found nothing and returned silently, re-opening the
      // panel and doing nothing else. Flushing keeps every store-only action in this switch
      // synchronous and observable (a deferred run would make `chat.clear` / `chat.new`
      // land a frame later, in the launcher hand-off too), while giving the DOM-walking one
      // a committed tree. Cheap: one navigation, only when a row names a surface.
      // (Braced: `const` in a bare case clause leaks into the whole switch.)
      const surface = intent.surface;
      if (surface) flushSync(() => openView(surface));
      runBindingById(intent.id);
      break;
    }
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
const _link = (
  id: string,
  label: string,
  keywords: string[],
  intent: NavIntent,
  /** A `registerKeybinding` id whose live combo the row advertises (ADR 0061) — for a
   *  deep-link that a keyboard shortcut ALSO reaches, so the row and Settings ▸ Keyboard
   *  never disagree and the deep-link doesn't need a twin row from `keybindingCommands`. */
  keybinding?: string,
) =>
  registerPaletteCommand({
    id,
    label,
    group: "Commands",
    keywords,
    keybinding,
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
// ⌘, opens the same dialog (the `settings.open` binding), so this row advertises it rather
// than `keybindingCommands` shipping a second "Open Settings" row beside it.
_link(
  "settings",
  "Settings",
  ["settings", "config", "preferences", "options"],
  { kind: "global" },
  "settings.open",
);
_link("box:fleet", "Settings: Fleet", ["fleet", "agents", "box"], { kind: "global", section: "fleet" });
_link("box:telemetry", "Settings: Telemetry", ["telemetry", "metrics", "box", "global"], {
  kind: "global",
  section: "telemetry",
});
// The screen that REBINDS a chord. The keyboard rows below teach an operator which chord runs
// what — the first half of a question whose second half ("that one's wrong, change it") had no
// palette row at all: Settings ▸ Keyboard was reachable only by opening Settings and finding
// the section. It carries `SHORTCUT_KEYWORDS`, the same tail as the rows it explains, so ONE
// `shortcuts` query returns the whole keyboard surface and the way to rebind it. `box:` + the
// section id, like its two siblings above — the section is `keybindings` (labelled "Keyboard"):
// grep `id: "keybindings"` under `settings/`, which is where the section table lives. It is
// neither flag- nor host-gated, so this row resolves in a sister agent's window too.
_link(
  "box:keybindings",
  "Settings: Keyboard",
  ["settings", "rebind", "remap", "chord", "combo", ...SHORTCUT_KEYWORDS],
  { kind: "global", section: "keybindings" },
);
// Keyboard actions as commands (ADR 0063 × ADR 0061): the triaged allow-list of registered
// bindings, each row RUNNING its binding's action and ADVERTISING that binding's live combo.
// `navigate` is handed in rather than imported so `keybindingCommands` has no runtime edge
// back to this module — and so a test can assert the exact intent a row emits.
registerKeybindingCommands(navigate);

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

/** Build the palette registry from the resolved view list + the inline plugin views.
 *  Stable across renders; nav commands + inline views re-register only when their set
 *  changes (plugins enable/disable) — matching the DS registry's add/withdraw model. */
export function usePaletteRegistry(
  views: View[],
  inlineViews: InlinePluginView[] = [],
  chat?: PaletteChatConfig,
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
    // Dynamic sources, served per palette read. Wired only when a fork registered one: the DS
    // shows its "Searching…" spinner whenever ANY provider declares `getCommands`, and core
    // ships zero sources — so an unconditional provider would put a 120ms spinner in front of
    // every keystroke in the default console. Registering a source bumps `seamVersion`, which
    // re-runs this effect and wires the provider then.
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
