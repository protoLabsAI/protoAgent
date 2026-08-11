import { Banner, Button } from "@protolabsai/ui/primitives";

import type { RuntimeStatus } from "../lib/types";
import { useUI } from "../state/uiStore";

/** Signed-out strip (#2513): an intentional OAuth disconnect used to present as a
 *  broken startup — a ~45s "Starting…" gate, then "Continue anyway", with the
 *  reconnect control buried and Chat still accepting sends that failed locally.
 *
 *  The server already reports the state precisely (`graph_auth_error` under
 *  `setup_complete`, #2458); this banner is the console half: a visible signed-out
 *  status the moment the runtime poll reports it, with the reconnect control in
 *  view (Settings → Model, where the OAuth account section lives). Self-clears on
 *  the same poll once reconnect rebuilds the graph. Renders nothing when signed in. */
export function SignedOutBanner({ runtime }: { runtime: RuntimeStatus | null }) {
  const openGlobalSettings = useUI((s) => s.openGlobalSettings);
  const err = runtime?.graph_auth_error;
  if (!err || runtime?.graph_loaded) return null;

  return (
    <Banner
      tone="warning"
      title="signed out"
      className="shell-warning-banner signed-out-banner"
      action={
        <Button size="sm" variant="primary" onClick={() => openGlobalSettings("model")}>
          Reconnect
        </Button>
      }
    >
      {err.message || `Signed out of ${err.provider} — reconnect to use chat.`}
    </Banner>
  );
}
