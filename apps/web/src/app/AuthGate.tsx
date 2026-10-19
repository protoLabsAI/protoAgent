import { Button } from "@protolabsai/ui/primitives";
import { Input, SecretInput } from "@protolabsai/ui/forms";
import { Dialog } from "@protolabsai/ui/overlays";
import { useQueryClient } from "@tanstack/react-query";
import { useId, useState, useSyncExternalStore } from "react";

import { authRequired, clearAuthRequired, saveAuthToken, subscribeAuth } from "../lib/auth";
import {
  claimPairingCode,
  describePairingError,
  formatPairingCode,
  isCompletePairingCode,
} from "../lib/pairing";

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
//
// "Have a pairing code?" (ADR 0087 D3, amended 2026-10-18): a phone that can't scan the
// Devices QR opens the console, lands here, and TYPES the short code shown beside the QR (or
// printed by `protoagent pair --device`). That claims a per-device token — stored exactly the
// way a `#pair=` link stores it (`claimPairingCode`) — so nobody has to copy the operator's
// shared bearer onto a phone. It's the same blocking dialog in a second mode, not a way out.

type Mode = "token" | "code";

export function AuthGate() {
  const needed = useSyncExternalStore(subscribeAuth, authRequired);
  const [mode, setMode] = useState<Mode>("token");
  const [token, setToken] = useState("");
  const [code, setCode] = useState("");
  const [claiming, setClaiming] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const codeId = useId();
  const codeHintId = useId();

  if (!needed) return null;

  const switchMode = (next: Mode) => {
    setMode(next);
    setSaveError(null);
  };

  const pair = async () => {
    // The server is authoritative, but an incomplete code is a guaranteed miss — and every
    // miss spends one of the 5 per 5 minutes the claim throttle allows (ADR 0121 D3).
    if (!isCompletePairingCode(code) || claiming) return;
    setClaiming(true);
    setSaveError(null);
    const res = await claimPairingCode(code);
    setClaiming(false);
    if (!res.ok) {
      setSaveError(describePairingError(res));
      return;
    }
    setCode("");
    clearAuthRequired();
    void queryClient.invalidateQueries();
  };

  const connect = () => {
    const res = saveAuthToken(token);
    if (!res.ok) {
      // The browser couldn't keep the token (ADR 0114 D1) — say so; don't pretend.
      setSaveError(res.error);
      return;
    }
    setSaveError(null);
    setToken("");
    // Refetch everything (including the boot probe) with the new bearer.
    void queryClient.invalidateQueries();
  };

  return (
    <Dialog
      open
      title="Authentication required"
      width={420}
      padding="roomy"
      className="auth-dialog"
      footer={
        mode === "token" ? (
          <>
            <Button type="button" variant="ghost" onClick={() => switchMode("code")}>
              Have a pairing code?
            </Button>
            <Button type="button" disabled={!token.trim()} onClick={connect}>
              Connect
            </Button>
          </>
        ) : (
          <>
            <Button type="button" variant="ghost" onClick={() => switchMode("token")} disabled={claiming}>
              Use a token instead
            </Button>
            <Button type="button" disabled={!isCompletePairingCode(code)} loading={claiming} onClick={() => void pair()}>
              Pair this device
            </Button>
          </>
        )
      }
    >
      {mode === "code" ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void pair();
          }}
        >
          <p>
            Enter the code shown beside the QR in <strong>Settings ▸ Devices ▸ Add a device</strong>{" "}
            on your computer, or printed by <code>protoagent pair --device</code>. This device gets
            its own token, which you can revoke there.
          </p>
          <label htmlFor={codeId} className="auth-gate-code-label">
            Pairing code
          </label>
          <Input
            id={codeId}
            className="auth-gate-code"
            autoFocus
            value={code}
            // Formatted AS TYPED (upper-cased, dash after five) so it reads back exactly like
            // the screen it was copied from. Look-alikes (O/0, I/L/1) are folded server-side.
            onChange={(e) => setCode(formatPairingCode(e.target.value))}
            placeholder="XXXXX-XXXXX"
            aria-describedby={codeHintId}
            autoComplete="one-time-code"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            inputMode="text"
            maxLength={16}
            data-testid="auth-pair-code"
          />
          <p id={codeHintId} className="auth-gate-hint">
            10 letters and numbers. Codes last 5 minutes and work once.
          </p>
          {saveError && (
            <p className="auth-gate-error" role="alert" style={{ color: "var(--pl-color-status-error)" }}>
              {saveError}
            </p>
          )}
        </form>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (token.trim()) connect();
          }}
        >
          <p>
            This instance is token-gated. Paste the operator token to connect — it&apos;s kept in
            this browser only, which is why a new window (or the desktop app) asks again.
          </p>
          {/* Someone who never had the token needs to know WHERE it lives, not just that one
              exists. `A2A_AUTH_TOKEN` alone is a dead end when the token was written to
              secrets.yaml — e.g. minted by the Devices pairing flow, which is exactly the case
              that locks a second client out. */}
          <p className="auth-gate-hint">
            It&apos;s the server&apos;s <code>A2A_AUTH_TOKEN</code>, or <code>auth.token</code> in
            its <code>secrets.yaml</code> (under the instance&apos;s <code>config/</code>).
          </p>
          <SecretInput
            autoFocus
            placeholder="operator token"
            aria-label="Operator token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          {saveError && (
            <p className="auth-gate-error" role="alert" style={{ color: "var(--pl-color-status-error)" }}>
              {saveError}
            </p>
          )}
        </form>
      )}
    </Dialog>
  );
}
