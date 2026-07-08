import { Button } from "@protolabsai/ui/primitives";
import { SecretInput } from "@protolabsai/ui/forms";
import { Dialog } from "@protolabsai/ui/overlays";
import { useQueryClient } from "@tanstack/react-query";
import { useState, useSyncExternalStore } from "react";

import { authRequired, saveAuthToken, subscribeAuth } from "../lib/auth";

// Token prompt for token-gated deployments (#873): any 401 (panel query, boot
// probe, chat turn) trips the auth store and this dialog appears — previously the
// only signal was per-panel "401 Unauthorized" cards, and writing
// `protoagent.authToken` required devtools. Saving invalidates every query so the
// app recovers in place, no reload.
// (localStorage as the token home is the standing posture — the httpOnly-cookie
// move is #869's call, not this gate's.)
//
// Blocking modal (#1921): a standing 401 leaves every panel dead, so this gate must
// NOT be bypassable — while it's up, the app behind it is unusable. We render it
// WITHOUT an `onClose`, which is the DS Dialog's non-dismissible contract: no
// backdrop-click close, no Escape close, no `×` button, and no "Not now" bail-out.
// It's paired with an opaque `.auth-dialog` scrim (see `.pl-overlay:has(.auth-dialog)`
// in theme.css) that fully obscures the dead UI behind it. The only exit is
// authenticating (or a background retry succeeding once the server stops 401ing,
// which clears the store via saveAuthToken → clearAuthRequired). This blocking
// posture is scoped to THIS dialog only — normal settings dialogs keep their onClose
// and stay Escape/backdrop-dismissible.

export function AuthGate() {
  const needed = useSyncExternalStore(subscribeAuth, authRequired);
  const [token, setToken] = useState("");
  const queryClient = useQueryClient();

  if (!needed) return null;

  const connect = () => {
    saveAuthToken(token);
    setToken("");
    // Refetch everything (including the boot probe) with the new bearer.
    void queryClient.invalidateQueries();
  };

  return (
    <Dialog
      open
      title="Authentication required"
      width={420}
      className="auth-dialog"
      footer={
        <Button type="button" disabled={!token.trim()} onClick={connect}>
          Connect
        </Button>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (token.trim()) connect();
        }}
      >
        <p>
          This instance is token-gated. Paste the operator token (the server's{" "}
          <code>A2A_AUTH_TOKEN</code>) to connect — it's kept in this browser only.
        </p>
        <SecretInput
          autoFocus
          placeholder="operator token"
          aria-label="Operator token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
      </form>
    </Dialog>
  );
}
