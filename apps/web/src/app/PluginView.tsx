import { AlertTriangle, Loader2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { apiUrl, authToken } from "../lib/api";
import { FederatedView } from "../plugins/FederatedView";
import { StageSubnav } from "./StageSubnav";
import type { PluginView as PluginViewType } from "../lib/types";

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
  // Reset load state when the loaded page changes (tab switch).
  useEffect(() => {
    setLoaded(false);
    setFailed(false);
  }, [src]);

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

  // ADR 0034 — a `ui: react` view mounts a federated React remote into the host tree (one React,
  // shared query cache) instead of an iframe. The trust gate (D5) is enforced here: only a
  // host-trusted plugin gets the in-process mount; an untrusted `ui: react` view falls through to
  // the sandboxed iframe of its `path` below. Trust is host-decided (never the plugin's word).
  if (view.ui === "react" && view.remote && view.trusted) {
    return (
      <section className="panel stage-panel plugin-view">
        <div className="plugin-view-body">
          <FederatedView label={view.label} remote={view.remote} />
        </div>
      </section>
    );
  }

  return (
    <>
      {/* Sub-tab strip above the panel card — shared StageSubnav (single source of truth). */}
      <StageSubnav active={activeTab} onSelect={setActiveTab}
                   tabs={tabs.map((t) => ({ id: t.id, label: t.label }))} />
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
