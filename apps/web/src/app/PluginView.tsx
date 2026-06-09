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
// console look (ADR 0026 theming bridge).
function consoleTheme(): Record<string, string> {
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
  const [failed, setFailed] = useState(false);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const pluginId = useMemo(() => pluginIdFromPath(view.path), [view.path]);
  // Reset load state when the loaded page changes (tab switch).
  useEffect(() => {
    setLoaded(false);
    setFailed(false);
  }, [src]);

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
      {/* Sub-tab strip above the panel card — shared DS Tabs (single source of truth). */}
      <Tabs active={activeTab} onSelect={setActiveTab}
            items={tabs.map((t) => ({ id: t.id, label: t.label }))} />
      <section className="panel stage-panel plugin-view">
      <div className="plugin-view-body">
        {failed ? (
          <div className="plugin-view-state" role="alert">
            <AlertTriangle size={18} />
            <span>Couldn’t load “{view.label}”. The plugin page at <code>{src}</code> didn’t respond.</span>
          </div>
        ) : (
          <>
            {!loaded ? (
              <div className="plugin-view-state">
                <Loader2 className="spin" size={18} />
                <span>Loading {view.label}…</span>
              </div>
            ) : null}
            <iframe
              ref={frameRef}
              className="plugin-view-frame"
              src={apiUrl(src)}
              title={view.label}
              sandbox="allow-scripts allow-forms allow-same-origin"
              onLoad={handleLoad}
              onError={() => setFailed(true)}
              style={{ visibility: loaded ? "visible" : "hidden" }}
            />
          </>
        )}
      </div>
      </section>
    </>
  );
}
