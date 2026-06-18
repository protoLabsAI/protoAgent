import { AlertTriangle, Loader2 } from "lucide-react";
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
  const pluginId = useMemo(() => pluginIdFromPath(view.path), [view.path]);

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
      if (m.type === "protoagent:subscribe" && Array.isArray(m.patterns)) {
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
    };
  }, [src, pluginId]);

  function handleLoad(e: React.SyntheticEvent<HTMLIFrameElement>) {
    setLoaded(true);
    const win = e.currentTarget.contentWindow;
    if (!win) return;
    // Hand the page the bearer + theme AFTER load — same origin, targeted, not in
    // the URL. The plugin page listens for `message` and uses them.
    try {
      const origin = new URL(apiUrl(src), window.location.href).origin;
      win.postMessage(
        { type: "protoagent:init", token: authToken() || null, theme: consoleTheme() },
        origin,
      );
    } catch {
      /* cross-origin / detached — best effort */
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
                <Loader2 className="spin" size={18} />
                <span>Loading {view.label}…</span>
              </div>
            ) : null}
            {/* Mount the iframe ONLY after the status probe confirms the route serves —
                a 404 fires onLoad (not onError), so an unprobed iframe would render the
                server's raw 404 body as a blank "view". */}
            {reachable ? (
              // sandbox: allow-popups (+ -to-escape-sandbox) so links / window.open inside
              // a plugin open as normal un-sandboxed pages instead of being blocked.
              // allow: clipboard via Permissions-Policy (no sandbox token exists for it) so
              // copy/paste works in plugin UIs.
              <iframe
                ref={frameRef}
                className="plugin-view-frame"
                src={apiUrl(src)}
                title={view.label}
                sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox"
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
