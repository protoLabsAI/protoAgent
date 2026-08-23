import designTokens from "@protolabsai/design/tokens.json";
import { Spinner } from "@protolabsai/ui/data";
import { AlertTriangle, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { apiUrl, authToken } from "../lib/api";
import { registerContextMenu } from "../contextMenu/registry";
import { useContextMenuStore } from "../contextMenu/store";
import type { MenuEntry } from "../contextMenu/types";
import { onTopic, replaySince } from "../lib/events";
import { registerKeybinding } from "../ext/keybindingRegistry";
import { runForwardedCombo } from "../keybindings/useKeybindings";
import { createPluginEventRelay, parseSubscribe } from "../lib/pluginEventRelay";
import {
  parsePluginMenuOpen,
  parsePluginMenuRegistration,
  type PluginMenuEntry,
  type PluginMenuOpen,
} from "../lib/pluginContextMenu";
import { pluginViewIcon } from "../lib/pluginIcon";
import { parseForwardedKey, parsePluginKeybindings } from "../lib/pluginKeybindings";
import { useUI } from "../state/uiStore";
import { Tabs } from "@protolabsai/ui/navigation";
import type { PluginView as PluginViewType } from "../lib/types";

// The owning plugin's id — the namespace every page-declared thing is forced into: the
// topics it publishes (ADR 0039's no-cross-dependency clause), the chords it declares
// (#1457), and its context-menu item ids (#3030).
//
// The surface key App stamps on (`plugin:<id>:<viewId>`) is authoritative; the path is the
// fallback for a raw manifest-shaped view that never passed through App's mapping. A view
// PAGE serves from the public namespace — `/plugins/<id>/view` (ADR 0026); `/api/plugins/
// <id>/…` is the plugin's GATED DATA router — so the path form accepts both. Matching only
// the /api form (as this did before #3030) resolved to "" for every real view, which
// silently refused the whole keybinding batch and published page topics UNNAMESPACED.
export function pluginIdFromView(view: Pick<PluginViewType, "key" | "path">): string {
  const fromKey = view.key?.match(/^plugin:([^:]+):/)?.[1];
  if (fromKey) return fromKey;
  return view.path?.match(/\/(?:api\/)?plugins\/([^/]+)\b/)?.[1] ?? "";
}

// The context-menu type a plugin view's in-frame menu resolves under (#3030). ADR 0036
// keys menus by type, so giving each plugin its own — rather than letting pages register
// into `rail-surface` & co. — is what keeps one view's items out of every other menu.
export function pluginMenuType(pluginId: string): string {
  return `plugin-view:${pluginId}`;
}

// The `--pl-*` custom-property names the design package publishes, derived from its
// tokens.json exactly the way the DS build generates tokens.css: kebab-case each key
// path under a `--pl` prefix. The top-level `light` block is the light-MODE override
// set (same names, different values), not extra tokens — skip it. Exported so tests
// can pin the derived list against the shipped token set.
const kebab = (s: string) => s.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase();
function collectTokenVars(node: Record<string, unknown>, prefix: string, acc: string[]): string[] {
  for (const [key, value] of Object.entries(node)) {
    if (prefix === "--pl" && key === "light") continue;
    const name = `${prefix}-${kebab(key)}`;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      collectTokenVars(value as Record<string, unknown>, name, acc);
    } else {
      acc.push(name);
    }
  }
  return acc;
}
export const PL_TOKEN_VARS: readonly string[] = collectTokenVars(
  designTokens as Record<string, unknown>, "--pl", [],
);

// The active light/dark mode: the explicit `data-theme` force on <html> when the theme
// machinery set one (agentTheme.ts / the DS ThemePanel), else the OS preference.
function themeMode(): string {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced) return forced;
  try {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    return "dark";
  }
}

// Console theme forwarded to a plugin view so it can match the console look (ADR 0026
// theming bridge). One flat record, three layers:
//   • the original curated six keys (bg/bgPanel/fg/fgMuted/brand/border) — unchanged;
//     older plugin-kits bridge ONLY these onto --pl-* tokens, so they're the
//     backward-compat contract (#2225);
//   • the FULL computed --pl-* snapshot, keyed off @protolabsai/design's tokens.json —
//     the kit passes --pl-*-form keys straight onto the page's :root, so a view inherits
//     the operator's whole active theme (spacing, radii, status colors, fonts), not just
//     the six curated slots;
//   • `mode` — the current data-theme ("light"/"dark"), so a page can pick
//     mode-appropriate assets/color-scheme (an unknown key to older kits — ignored).
// Exported so the command palette (ADR 0057) can hand the same theme to an
// inline-morphed plugin iframe.
export function consoleTheme(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const s = getComputedStyle(document.documentElement);
  const g = (n: string) => s.getPropertyValue(n).trim();
  const theme: Record<string, string> = {
    bg: g("--bg"), bgPanel: g("--bg-panel"), fg: g("--fg"),
    fgMuted: g("--fg-muted"), brand: g("--brand-violet-light"), border: g("--border"),
    mode: themeMode(),
  };
  for (const name of PL_TOKEN_VARS) {
    const v = g(name);
    if (v) theme[name] = v; // an unresolvable var is omitted — the kit skips empties anyway
  }
  return theme;
}

// Host for a plugin-contributed console surface (ADR 0026): a same-origin iframe
// of the page the plugin serves, with optional view-tabs, a loading overlay, a
// failure fallback, and a post-load handshake that hands the page the operator
// bearer + theme tokens via postMessage (never a token in the URL).
// Mount with `key={view key}` so switching views resets state.
export function PluginView({ view }: { view: PluginViewType }) {
  const tabs = view.tabs ?? [];
  const [activeTab, setActiveTab] = useState(tabs[0]?.id ?? "");
  const src = useMemo(() => {
    const t = tabs.find((x) => x.id === activeTab);
    return t?.path ?? view.path;
  }, [tabs, activeTab, view.path]);

  const [loaded, setLoaded] = useState(false);
  // null = no error; a string = an actionable failure message to show in the panel.
  const [error, setError] = useState<string | null>(null);
  // Probed and reachable (HTTP ok) — only then do we mount the iframe. Until the probe
  // resolves we show the loading state; this keeps a 404 from ever rendering the server's
  // bare {"detail":"Not Found"} body as the "view".
  const [reachable, setReachable] = useState(false);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  // Pending init re-post timers (see handleLoad) — cleared on unmount / src change.
  const initTimers = useRef<number[]>([]);
  // Unregister fns for chords this view declared (#1457). Held in a ref so the message
  // handler can replace the set in place, and dropped on teardown — a keybinding that
  // outlived its iframe would post into a dead window.
  const pluginBindings = useRef<Array<() => void>>([]);
  // The item set the page declared with `protoagent:contextmenu:register` (#3030) — the
  // default a bare `:open` uses. Held in a ref so a re-register swaps it without a
  // re-render, and so the open path reads the CURRENT set rather than a captured one.
  const pluginMenuItems = useRef<PluginMenuEntry[]>([]);
  // Disarms the open menu's "focus went back into the frame" watcher (see openPluginMenu).
  const menuDismiss = useRef<(() => void) | null>(null);
  // Has the frame navigated to the plugin page? Gates the posts below: a mounted-but-not-yet
  // -navigated iframe is still on about:blank, which INHERITS the console's origin — under the
  // desktop app that's `tauri://localhost`, so a post targeted at the sidecar origin is refused
  // outright: "Unable to post message to http://127.0.0.1:7870. Recipient has origin
  // tauri://localhost." The frame had no listener yet either way, so the post was always a
  // no-op; gate it and drop the noise.
  //
  // Written at the LIFECYCLE EDGES, never mirrored off the `loaded` state during render. Two
  // things prove navigation, and BOTH are needed: the iframe's own load event, and any message
  // FROM the frame. The page's script runs (and posts its first `subscribe`) BEFORE the parent
  // sees `load` — so gating the `since` replay on load alone dropped it and broke reopen
  // catch-up (#1640). A message can only come from the navigated page; about:blank has no
  // script. Reset when the frame is re-pointed.
  const navigatedRef = useRef(false);
  const pluginId = useMemo(() => pluginIdFromView(view), [view.key, view.path]);
  // Background delivery (#1640): a `background: true` subscribe from the page asks App
  // to keep this view mounted (hidden) when another surface is active. Store-reported;
  // App owns the mount policy.
  const setPluginBackground = useUI((s) => s.setPluginBackground);

  // Post the bearer + theme to the iframe. Idempotent on the kit side (applyTheme just
  // re-sets CSS vars), so it's safe to call repeatedly — which the handshake relies on.
  const postInit = (win: Window) => {
    try {
      const origin = new URL(apiUrl(src), window.location.href).origin;
      win.postMessage({ type: "protoagent:init", token: authToken() || null, theme: consoleTheme() }, origin);
    } catch {
      /* cross-origin / detached — best effort */
    }
  };

  // Open the console's context menu (ADR 0036) for a right-click the PAGE reported (#3030).
  //
  // The page owns the event — a right-click inside the frame never bubbles to the host, and
  // under the desktop app the frame is cross-origin, so the host can't listen on its document
  // either. So the page calls preventDefault() and posts `:open` with the cursor in its own
  // viewport; we translate through the iframe's rect and open the menu there, clamped INTO
  // the frame (a plugin doesn't get to place a console menu over the console's chrome).
  const openPluginMenu = (req: PluginMenuOpen) => {
    const frame = frameRef.current;
    if (!frame || !pluginId) return;
    const rect = frame.getBoundingClientRect();
    // A view kept mounted for background delivery (#1640) is `display: none`'d by App, so
    // its frame measures 0×0. Opening its menu would drop it in the console's top-left,
    // over whatever surface IS showing — an off-screen page doesn't get to pop menus.
    if (!rect.width || !rect.height) return;
    // Items for THIS menu if the page sent some, else whatever it registered earlier.
    const specs = req.entries ?? pluginMenuItems.current;
    const origin = (() => {
      try {
        return new URL(apiUrl(src), window.location.href).origin;
      } catch {
        return window.location.origin;
      }
    })();
    const entries: MenuEntry[] = specs.map((spec) =>
      "divider" in spec
        ? { id: spec.id, divider: true }
        : {
            id: spec.id,
            label: spec.label,
            // A NAME, resolved against the console's own icon set — no plugin markup.
            icon: spec.icon ? pluginViewIcon(spec.icon, 14) : undefined,
            danger: spec.danger,
            disabled: spec.disabled,
            run: (_ctx, helpers) => {
              helpers.close();
              try {
                // Fire back with the id the PAGE knows, not our namespaced one.
                frameRef.current?.contentWindow?.postMessage(
                  { type: "protoagent:contextmenu:action", itemId: spec.pluginLocalId },
                  origin,
                );
              } catch {
                /* cross-origin / detached — best effort */
              }
            },
          },
    );
    // The plugin's own affordances, then the console's. Configure… is the same action the
    // rail icon's menu offers (ADR 0036 D6) — a view that suppressed the browser menu should
    // never leave the operator with an EMPTY menu, so this is appended even for an empty set.
    if (entries.length) entries.push({ id: "plugin-view-div", divider: true });
    entries.push({
      id: "plugin-view-configure",
      label: "Configure…",
      icon: <SlidersHorizontal size={14} />,
      // The view's label stands in for the plugin's display name — App resolves the real
      // one from runtime status for the rail menu, which isn't worth a query from here.
      run: () => useUI.getState().openPluginConfig(pluginId, view.label),
    });

    useContextMenuStore.getState().openMenu(
      pluginMenuType(pluginId),
      rect.left + Math.min(Math.max(req.x, 0), rect.width),
      rect.top + Math.min(Math.max(req.y, 0), rect.height),
      { entries },
    );

    // A click back inside the frame is invisible to the host's dismiss listener (it's another
    // document), so the menu would hang there until Escape. Focus moving into the frame DOES
    // blur the host window — close on that. Armed a tick late so the menu taking focus on
    // open doesn't immediately trip it.
    menuDismiss.current?.();
    const onBlur = () => {
      useContextMenuStore.getState().close();
      disarm();
    };
    const disarm = () => {
      window.removeEventListener("blur", onBlur);
      menuDismiss.current = null;
    };
    const armed = window.setTimeout(() => window.addEventListener("blur", onBlur), 0);
    menuDismiss.current = () => {
      clearTimeout(armed);
      disarm();
    };
  };

  // This view's menu type exists only while the view is mounted. `items` reads the entries
  // off the ctx the open passes, so the resolved menu is always the one built for THAT
  // right-click — including the frame-specific `run` closures that post the action back.
  useEffect(() => {
    if (!pluginId) return;
    return registerContextMenu({
      type: pluginMenuType(pluginId),
      items: (ctx: { entries?: MenuEntry[] } | undefined) => ctx?.entries ?? [],
    });
  }, [pluginId]);

  // Probe the view URL before mounting the iframe. A same-origin HTTP error (a 404 from an
  // unmounted /api/plugins/<id>/<view>, FastAPI's {"detail":"Not Found"}) fires the iframe's
  // onLoad — NOT onError — so trusting onLoad would render the raw 404 as a blank panel. We
  // must read res.status. On !ok we phrase the cause from the owning plugin's load state:
  //   • plugin reported an error (missing env / deps not installed) → surface it verbatim
  //   • enabled but not loaded → the view route isn't serving yet (mount race / restart)
  //   • otherwise → the HTTP status. One retry covers a sub-second race with a hot-mount reload.
  useEffect(() => {
    let cancelled = false;
    navigatedRef.current = false; // re-pointed frame: back to about:blank until it navigates
    setLoaded(false);
    setError(null);
    setReachable(false);

    function describeFailure(status: number | null): string {
      if (view.pluginError) return view.pluginError;
      if (view.pluginLoaded === false)
        return `The plugin view at ${src} isn’t mounted yet. If you just enabled it, give it a moment — or restart the server to finish enabling.`;
      if (status != null) return `The plugin page at ${src} returned HTTP ${status}.`;
      return `The plugin page at ${src} didn’t respond.`;
    }

    async function probe(attempt: number): Promise<void> {
      try {
        const res = await fetch(apiUrl(src), {
          headers: { ...(authToken() ? { Authorization: `Bearer ${authToken()}` } : {}) },
        });
        if (cancelled) return;
        if (res.ok) {
          setReachable(true);
          return;
        }
        // Retry once on a server-side miss — covers the brief window where the rail
        // renders the view before the hot-mount include_router commits (#822 reload race).
        if (attempt === 0 && (res.status === 404 || res.status >= 500)) {
          setTimeout(() => void probe(1), 600);
          return;
        }
        setError(describeFailure(res.status));
      } catch {
        if (cancelled) return;
        // True network/CORS failure (connection refused, blocked) — no status to read.
        if (attempt === 0) {
          setTimeout(() => void probe(1), 600);
          return;
        }
        setError(describeFailure(null));
      }
    }

    void probe(0);
    return () => {
      cancelled = true;
    };
  }, [src, view.pluginLoaded, view.pluginError]);

  // Event-bus relay across the sandbox (ADR 0039, extended #1640). The page subscribes via
  // `protoagent:subscribe {patterns, since?, background?}`; the host forwards matching bus
  // events in (`protoagent:event {topic, data, seq}`) and accepts `protoagent:publish
  // {topic,data}` back, forcing the topic into this plugin's namespace before POSTing.
  // `since` triggers an immediate replay of retained ring-buffer events newer than that seq
  // (the console's client-side mirror — lib/events.ts), so a freshly (re)mounted page can
  // catch up on what it missed instead of polling; `seq` on every relayed frame is the
  // page's high-water mark for its next `since`. Normally only the *visible* plugin's
  // iframe is mounted, so the relay is scoped to it; `background: true` asks App (via the
  // ui store) to keep this view mounted-but-hidden so delivery continues off-screen.
  useEffect(() => {
    const origin = (() => {
      try {
        return new URL(apiUrl(src), window.location.href).origin;
      } catch {
        return window.location.origin;
      }
    })();
    // Pattern matching + since-replay + seq dedupe live in the pure relay
    // (lib/pluginEventRelay.ts) — this effect only wires postMessage to it.
    const relay = createPluginEventRelay({
      post: (frame) => {
        if (!navigatedRef.current) return; // pre-navigation frame — see navigatedRef
        frameRef.current?.contentWindow?.postMessage({ type: "protoagent:event", ...frame }, origin);
      },
      replaySince,
    });

    const onWindowMessage = (e: MessageEvent) => {
      // Only trust messages from THIS iframe's window.
      if (!frameRef.current || e.source !== frameRef.current.contentWindow) return;
      // It spoke, so it's the navigated plugin page — not the about:blank placeholder. This
      // is what unblocks the `since` replay below: the page's first `subscribe` beats the
      // parent's load event, and the replay posts back inside this very handler.
      navigatedRef.current = true;
      const m = e.data || {};
      if (m.type === "protoagent:ready") {
        // The kit announced it's now listening. It registers its `message` handler
        // asynchronously (dynamic import of the plugin-kit), so the load-time init post
        // can race ahead of it and be dropped — leaving the view on the kit's default
        // theme until a manual switch. Re-send the bearer + theme now that we know it's
        // listening, so it themes immediately. (Older kits don't ping; handleLoad's
        // retry covers those.)
        if (frameRef.current?.contentWindow) postInit(frameRef.current.contentWindow);
      } else if (m.type === "protoagent:subscribe") {
        const req = parseSubscribe(m);
        if (!req) return;
        // Hidden-delivery opt-in/out (#1640) — only an explicit boolean toggles it, so
        // pre-#1640 subscribes (no `background` field) never touch the mount policy.
        if (req.background !== undefined && view.key) setPluginBackground(view.key, req.background);
        relay.subscribe(req);
      } else if (m.type === "protoagent:keybindings") {
        // The page declares chords it wants (#1457). Ids are namespaced in the parser —
        // a view can't register or replace a core binding, or collide with another
        // plugin. The chord it names is a DEFAULT: the operator's override wins through
        // the same Settings ▸ Keyboard path as everything else.
        const specs = parsePluginKeybindings(m, pluginId);
        if (!specs) return;
        // Re-registering REPLACES the previous set, so a view that drops a chord doesn't
        // leave a ghost binding firing into a page that forgot about it.
        pluginBindings.current.forEach((off) => off());
        pluginBindings.current = specs.map((spec) =>
          registerKeybinding({
            id: spec.id,
            label: spec.label,
            group: spec.group,
            defaultKeys: spec.defaultKeys,
            run: () => {
              // Fire back with the id the PAGE knows, not our namespaced one.
              frameRef.current?.contentWindow?.postMessage(
                { type: "protoagent:keybinding", id: spec.pluginLocalId },
                "*",
              );
            },
          }),
        );
      } else if (m.type === "protoagent:contextmenu:register") {
        // The page declares a DEFAULT item set (#3030). Ids are namespaced in the parser —
        // a view can't register over a core menu item or collide with another plugin. Like
        // the keybinding bridge, re-registering REPLACES the previous set (`items: []`
        // clears it), so a dropped item doesn't linger as a ghost entry.
        const entries = parsePluginMenuRegistration(m, pluginId);
        if (entries) pluginMenuItems.current = entries;
      } else if (m.type === "protoagent:contextmenu:open") {
        // "The operator right-clicked here" — the only way the host learns about a
        // right-click inside the frame. Items may ride along (a menu for whatever is under
        // the cursor) or be omitted to use the registered set.
        const req = parsePluginMenuOpen(m, pluginId);
        if (req) openPluginMenu(req);
      } else if (m.type === "protoagent:keydown") {
        // A chord the page didn't handle. Without this, every host shortcut was dead
        // while a plugin view had focus — the iframe is a separate document, so its
        // keydowns never reach the host listener at all.
        const key = parseForwardedKey(m);
        if (key) runForwardedCombo(key.combo, key.editable);
      } else if (m.type === "protoagent:publish" && typeof m.topic === "string") {
        // Force the plugin's namespace — a page can only publish under its own id.
        const bare = m.topic.replace(/^.*?\./, "");
        const topic = pluginId ? `${pluginId}.${bare}` : m.topic;
        void fetch(apiUrl("/api/events/publish"), {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(authToken() ? { Authorization: `Bearer ${authToken()}` } : {}),
          },
          body: JSON.stringify({ topic, data: m.data || {} }),
        }).catch(() => {});
      }
    };
    window.addEventListener("message", onWindowMessage);

    const off = onTopic("#", (data, topic, seq) => relay.deliver(topic, data, seq));

    return () => {
      window.removeEventListener("message", onWindowMessage);
      off();
      // Drop any pending init re-posts — the iframe is being torn down / re-pointed.
      initTimers.current.forEach(clearTimeout);
      initTimers.current = [];
      // …and the chords it registered, for the same reason.
      pluginBindings.current.forEach((off) => off());
      pluginBindings.current = [];
      // A menu whose items post into a frame that's going away must go with it — the
      // registration itself is dropped by its own effect.
      menuDismiss.current?.();
      pluginMenuItems.current = [];
      if (useContextMenuStore.getState().type === pluginMenuType(pluginId)) {
        useContextMenuStore.getState().close();
      }
    };
  }, [src, pluginId, view.key, setPluginBackground]);

  // Live re-theme (ADR 0026/0042). The console fires a `protoagent:theme` window event on
  // any theme/accent change (watchThemeChanges in agentTheme.ts observes the root's
  // style/data-theme). Re-post the FRESH theme payload to the mounted iframe so an embedded
  // plugin view repaints WITHOUT a reload — its plugin-kit listens for `protoagent:theme`
  // and re-skins the --pl-* tokens. `handleLoad` only covers the first paint; this covers
  // every subsequent switch. (consoleTheme() reads the now-updated :root vars at fire time.)
  useEffect(() => {
    const onThemeChange = () => {
      const win = frameRef.current?.contentWindow;
      // Not navigated yet → about:blank, wrong origin, nobody listening (see navigatedRef).
      // handleLoad posts the FRESH theme on load, so nothing is lost by skipping here.
      if (!win || !navigatedRef.current) return;
      try {
        const origin = new URL(apiUrl(src), window.location.href).origin;
        win.postMessage({ type: "protoagent:theme", theme: consoleTheme() }, origin);
      } catch {
        /* cross-origin / detached — best effort */
      }
    };
    window.addEventListener("protoagent:theme", onThemeChange);
    return () => window.removeEventListener("protoagent:theme", onThemeChange);
  }, [src]);

  function handleLoad(e: React.SyntheticEvent<HTMLIFrameElement>) {
    // Synchronously, BEFORE postInit: the page can answer with `subscribe` (and its `since`
    // replay) inside this same tick, well before the setLoaded re-render commits.
    navigatedRef.current = true;
    setLoaded(true);
    const win = e.currentTarget.contentWindow;
    if (!win) return;
    // Hand the page the bearer + theme AFTER load — same origin, targeted, not in the URL.
    // The plugin page registers its `message` listener asynchronously (dynamic import of the
    // plugin-kit), so this first post can land BEFORE the kit is listening and be dropped —
    // the view then renders with the kit's default theme until a manual theme switch (the
    // "toggle around for it to load" bug). So re-post on a short schedule; the retry lands
    // once the kit is ready, and postInit is idempotent so the extra posts are harmless. A
    // newer kit that pings `protoagent:ready` makes this exact (handled above); the retry is
    // the fallback for kits that only listen.
    initTimers.current.forEach(clearTimeout);
    initTimers.current = [];
    postInit(win);
    for (const ms of [100, 300, 700, 1500]) {
      initTimers.current.push(window.setTimeout(() => postInit(win), ms));
    }
  }

  // ADR 0038 — plugin views are sandboxed iframes (the plugin serves its own page). Module
  // Federation + the in-process `ui: react` path were retired; rich plugins serve their own UI.
  return (
    <>
      {/* Sub-tab strip above the panel card — only when there's more than one tab. A
          single-/no-tab view (e.g. Notes) has nothing to switch, so we skip the strip;
          rendering it anyway showed an empty <select> on mobile (responsive Tabs). */}
      {tabs.length > 1 && (
        <Tabs responsive active={activeTab} onSelect={setActiveTab}
              items={tabs.map((t) => ({ id: t.id, label: t.label }))} />
      )}
      <section className="panel stage-panel plugin-view">
      <div className="plugin-view-body">
        {error ? (
          <div className="plugin-view-state" role="alert">
            <AlertTriangle size={18} />
            <span>Couldn’t load “{view.label}”. {error}</span>
          </div>
        ) : (
          <>
            {!loaded ? (
              <div className="plugin-view-state">
                <Spinner size={18} />
                <span>Loading {view.label}…</span>
              </div>
            ) : null}
            {/* Mount the iframe ONLY after the status probe confirms the route serves —
                a 404 fires onLoad (not onError), so an unprobed iframe would render the
                server's raw 404 body as a blank "view". */}
            {reachable ? (
              // sandbox: allow-popups (+ -to-escape-sandbox) so links / window.open inside
              // a plugin open as normal un-sandboxed pages instead of being blocked.
              // Pointer lock needs BOTH the allow-pointer-lock sandbox token AND the
              // pointer-lock Permissions-Policy in `allow=` — and the policy has to be
              // delegated at EVERY nesting level, so the nested artifact iframe (which sets
              // its own allow="pointer-lock") can capture the mouse for games / canvas / 3D.
              // Esc always releases it.
              // allow: clipboard + pointer-lock via Permissions-Policy (no sandbox token
              // exists for clipboard) so copy/paste + pointer capture work in plugin UIs.
              // allow-downloads: a sandboxed frame cannot start a download without it, so
              // the artifact panel's Download button (ADR 0092 D2/D3 file artifacts — a
              // plain <a download>.click()) was refused outright: "Not allowed to download
              // due to sandboxing". Scoped to the plugin-view frame, which already carries
              // allow-same-origin + allow-scripts; the artifact plugin's NESTED frame stays
              // without it, so model-generated code still can't push a file at the operator.
              <iframe
                ref={frameRef}
                className="plugin-view-frame"
                src={apiUrl(src)}
                title={view.label}
                sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-pointer-lock allow-downloads"
                allow="clipboard-read; clipboard-write; pointer-lock"
                onLoad={handleLoad}
                onError={() => setError(`The plugin page at ${src} didn’t respond.`)}
                style={{ visibility: loaded ? "visible" : "hidden" }}
              />
            ) : null}
          </>
        )}
      </div>
      </section>
    </>
  );
}
