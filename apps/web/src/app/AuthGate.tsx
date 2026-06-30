import { Button } from "@protolabsai/ui/primitives";
import { SecretInput } from "@protolabsai/ui/forms";
import { Dialog } from "@protolabsai/ui/overlays";
import { useQueryClient } from "@tanstack/react-query";
import { useState, useSyncExternalStore } from "react";

import { authRequired, clearAuthRequired, saveAuthToken, subscribeAuth } from "../lib/auth";

// Token prompt for token-gated deployments (#873): any 401 (panel query, boot
// probe, chat turn) trips the auth store and this dialog appears — previously the
// only signal was per-panel "401 Unauthorized" cards, and writing
// `protoagent.authToken` required devtools. Saving invalidates every query so the
// app recovers in place, no reload. "Not now" dismisses; the next 401 re-prompts.
// (localStorage as the token home is the standing posture — the httpOnly-cookie
// move is #869's call, not this gate's.)

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
      onClose={clearAuthRequired}
      width={420}
      footer={
        <>
          <Button type="button" variant="ghost" onClick={clearAuthRequired}>
            Not now
          </Button>
          <Button type="button" disabled={!token.trim()} onClick={connect}>
            Connect
          </Button>
        </>
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
