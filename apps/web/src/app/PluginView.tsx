import { Spinner } from "@protolabsai/ui/data";
import { AlertTriangle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { apiUrl, authToken } from "../lib/api";
import { onTopic, topicMatches } from "../lib/events";
import { Tabs } from "@protolabsai/ui/navigation";
import type { PluginView as PluginViewType } from "../lib/types";

// Derive the plugin id from a view path (`/api/plugins/<id>/...`) — used to namespace-stamp
// any events the sandboxed page publishes (ADR 0039 — a plugin only publishes under its own
// namespace; the no-cross-dependency clause).
function pluginIdFromPath(path: string): string {
  return path.match(/\/api\/plugins\/([^/]+)\b/)?.[1] ?? "";
}

// Curated console theme tokens forwarded to a plugin view so it can match the
// console look (ADR 0026 theming bridge). Exported so the command palette (ADR 0057)
// can hand the same 6-key theme to an inline-morphed plugin iframe.
export function consoleTheme(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const s = getComputedStyle(document.documentElement);
  const g = (n: string) => s.getPropertyValue(n).trim();
  return {
    bg: g("--bg"), bgPanel: g("--bg-panel"), fg: g("--fg"),
    fgMuted: g("--fg-muted"), brand: g("--brand-violet-light"), border: g("--border"),
  };
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
  const pluginId = useMemo(() => pluginIdFromPath(view.path), [view.path]);

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

  // Probe the view URL before mounting the iframe. A same-origin HTTP error (a 404 from an
  // unmounted /api/plugins/<id>/<view>, FastAPI's {"detail":"Not Found"}) fires the iframe's
  // onLoad — NOT onError — so trusting onLoad would render the raw 404 as a blank panel. We
  // must read res.status. On !ok we phrase the cause from the owning plugin's load state:
  //   • plugin reported an error (missing env / deps not installed) → surface it verbatim
  //   • enabled but not loaded → the view route isn't serving yet (mount race / restart)
  //   • otherwise → the HTTP status. One retry covers a sub-second race with a hot-mount reload.
  useEffect(() => {
    let cancelled = false;
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

  // Event-bus relay across the sandbox (ADR 0039). The page subscribes via
  // `protoagent:subscribe {patterns}`; the host forwards matching bus events in
  // (`protoagent:event`) and accepts `protoagent:publish {topic,data}` back, forcing the
  // topic into this plugin's namespace before POSTing. Only the *visible* plugin's iframe
  // is mounted, so this relay is naturally scoped to it.
  useEffect(() => {
    const origin = (() => {
      try {
        return new URL(apiUrl(src), window.location.href).origin;
      } catch {
        return window.location.origin;
      }
    })();
    let patterns: string[] = [];

    function matches(topic: string): boolean {
      return patterns.some((p) => topicMatches(p, topic));
    }

    const onWindowMessage = (e: MessageEvent) => {
      // Only trust messages from THIS iframe's window.
      if (!frameRef.current || e.source !== frameRef.current.contentWindow) return;
      const m = e.data || {};
      if (m.type === "protoagent:ready") {
        // The kit announced it's now listening. It registers its `message` handler
        // asynchronously (dynamic import of the plugin-kit), so the load-time init post
        // can race ahead of it and be dropped — leaving the view on the kit's default
        // theme until a manual switch. Re-send the bearer + theme now that we know it's
        // listening, so it themes immediately. (Older kits don't ping; handleLoad's
        // retry covers those.)
        if (frameRef.current?.contentWindow) postInit(frameRef.current.contentWindow);
      } else if (m.type === "protoagent:subscribe" && Array.isArray(m.patterns)) {
        patterns = m.patterns.filter((p: unknown) => typeof p === "string");
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

    const off = onTopic("#", (data, topic) => {
      if (!matches(topic)) return;
      frameRef.current?.contentWindow?.postMessage(
        { type: "protoagent:event", topic, data },
        origin,
      );
    });

    return () => {
      window.removeEventListener("message", onWindowMessage);
      off();
      // Drop any pending init re-posts — the iframe is being torn down / re-pointed.
      initTimers.current.forEach(clearTimeout);
      initTimers.current = [];
    };
  }, [src, pluginId]);

  // Live re-theme (ADR 0026/0042). The console fires a `protoagent:theme` window event on
  // any theme/accent change (watchThemeChanges in agentTheme.ts observes the root's
  // style/data-theme). Re-post the FRESH curated theme to the mounted iframe so an embedded
  // plugin view repaints WITHOUT a reload — its plugin-kit listens for `protoagent:theme`
  // and re-skins the --pl-* tokens. `handleLoad` only covers the first paint; this covers
  // every subsequent switch. (consoleTheme() reads the now-updated :root vars at fire time.)
  useEffect(() => {
    const onThemeChange = () => {
      const win = frameRef.current?.contentWindow;
      if (!win) return;
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
              // allow-pointer-lock so a plugin (or a nested artifact iframe inside it) can
              // capture the mouse — needed for games / canvas / 3D; pointer lock must be
              // granted at EVERY nesting level, and Esc always releases it.
              // allow: clipboard via Permissions-Policy (no sandbox token exists for it) so
              // copy/paste works in plugin UIs.
              <iframe
                ref={frameRef}
                className="plugin-view-frame"
                src={apiUrl(src)}
                title={view.label}
                sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-pointer-lock"
                allow="clipboard-read; clipboard-write"
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
