import { AlertTriangle, Loader2 } from "lucide-react";
import { useState } from "react";

import { apiUrl } from "../lib/api";
import type { PluginView as PluginViewType } from "../lib/types";

// Host for a plugin-contributed console surface (ADR 0026): a same-origin iframe
// of the page the plugin serves, with a loading overlay and a failure fallback.
// Mount with `key={view key}` so switching views resets load state.
export function PluginView({ view }: { view: PluginViewType }) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);

  return (
    <section className="panel stage-panel plugin-view">
      {failed ? (
        <div className="plugin-view-state" role="alert">
          <AlertTriangle size={18} />
          <span>Couldn’t load “{view.label}”. The plugin page at <code>{view.path}</code> didn’t respond.</span>
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
            src={apiUrl(view.path)}
            title={view.label}
            sandbox="allow-scripts allow-forms allow-same-origin"
            onLoad={() => setLoaded(true)}
            onError={() => setFailed(true)}
            style={{ visibility: loaded ? "visible" : "hidden" }}
          />
        </>
      )}
    </section>
  );
}
