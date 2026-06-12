import { Button } from "@protolabsai/ui/primitives";
import { AlertTriangle, RefreshCw, Trash2 } from "lucide-react";

import "./app-crash.css";

// Full-page fallback for the ROOT error boundary (#872) — a render throw that
// escapes every panel boundary lands here instead of a white screen. This renders
// while the app is broken, so it must stay dependency-light: no stores, no
// queries, no router — any of them may be the thing that threw.

/** Clear the persisted chat sessions (all agents' slug-suffixed keys included) but
 *  keep layout/theme/authToken — the same selective scope as the tenant guard. A
 *  corrupt saved session is the known way to brick render (issue #872); everything
 *  else persisted is cheap to keep. */
export function resetChatData() {
  try {
    const stale: string[] = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i) || "";
      if (k.startsWith("protoagent.chat.sessions")) stale.push(k);
    }
    stale.forEach((k) => window.localStorage.removeItem(k));
  } catch {
    // Storage unavailable — reload alone is the best we can do.
  }
}

export function AppCrash({ error }: { error: Error }) {
  return (
    <div className="app-crash" role="alert">
      <AlertTriangle size={28} aria-hidden />
      <h1>The console hit a render error</h1>
      <p className="app-crash__msg">{error.message}</p>
      <div className="app-crash__actions">
        <Button type="button" onClick={() => window.location.reload()}>
          <RefreshCw size={14} /> Reload
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={() => {
            resetChatData();
            window.location.reload();
          }}
        >
          <Trash2 size={14} /> Reset chat data &amp; reload
        </Button>
      </div>
      <p className="app-crash__hint">
        Layout, theme and auth token are kept. If reloading loops back here, reset chat
        data — a corrupt saved session is the usual cause.
      </p>
    </div>
  );
}
