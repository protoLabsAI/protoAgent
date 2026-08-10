import { FormField, Input } from "@protolabsai/ui/forms";
import { Button, Callout } from "@protolabsai/ui/primitives";
import { Alert, Spinner } from "@protolabsai/ui/data";
import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";

// Native OAuth account lifecycle (#2460) — ONE implementation of status /
// sign-in (device + redirect) / cancel / disconnect, shared by the Setup Wizard
// and Settings ▸ Model so the two can never drift. The wizard consumes the hook
// with its own JSX (it interleaves model probing); Settings renders the
// standard <OAuthAccountCard>. Post-transition, the entire query cache is
// invalidated (the #2462 pattern): a reconnect rebuilds the server graph inline
// (#2458's graph_reloaded) and a disconnect unloads it (#2459's
// graph_unloaded), so every visible model/provider/runtime surface must
// converge without a restart.

export const OAUTH_PROVIDER_LABEL: Record<string, string> = {
  "anthropic-oauth": "Claude subscription",
  "openai-codex": "ChatGPT subscription",
};

export type OauthProviderStatus = { signed_in: boolean; detail: string; hint: string };

export type OauthLogin = {
  provider: string;
  mode: "device" | "redirect";
  flowId: string;
  userCode?: string;
  verifyUri?: string;
  authorizeUrl?: string;
};

export function useOauthLifecycle(opts?: { onSignedIn?: () => void; onDisconnected?: () => void }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Record<string, OauthProviderStatus>>({});
  const [login, setLogin] = useState<OauthLogin | null>(null);
  const [loginCode, setLoginCode] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState("");
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The callbacks live in a ref so the poll timer never closes over a stale render.
  const cbRef = useRef(opts);
  cbRef.current = opts;

  const refreshStatus = useCallback(async () => {
    try {
      const r = await api.oauthStatus();
      const map: Record<string, OauthProviderStatus> = {};
      for (const p of r.providers) map[p.provider] = { signed_in: p.signed_in, detail: p.detail, hint: p.hint };
      setStatus(map);
    } catch {
      /* status is advisory — a failed probe just leaves the line blank */
    }
  }, []);

  const clearLogin = useCallback(() => {
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
    setLogin(null);
    setLoginCode("");
  }, []);
  useEffect(() => () => clearLogin(), [clearLogin]);

  const onSignedIn = useCallback(() => {
    clearLogin();
    setLoginError("");
    // The sign-in may have rebuilt the server graph inline (#2458) — converge
    // every server-derived query (runtime status, settings, models) now.
    void queryClient.invalidateQueries();
    void refreshStatus();
    cbRef.current?.onSignedIn?.();
  }, [clearLogin, queryClient, refreshStatus]);

  // Cancel an in-progress sign-in — cancel the SERVER flow too, not just the local
  // timer (#2440), so the pending device/PKCE flow can't be completed later.
  const cancelSignIn = useCallback(() => {
    const flowId = login?.flowId;
    clearLogin();
    setLoginError("");
    if (flowId) void api.oauthCancel(flowId).catch(() => {});
  }, [login, clearLogin]);

  const pollDevice = useCallback(
    (flowId: string, intervalMs: number) => {
      pollRef.current = setTimeout(async () => {
        try {
          const r = await api.oauthPoll(flowId);
          if (r.status === "complete") {
            if (r.graph_reload_error) setLoginError(`Signed in, but the agent did not restart cleanly: ${r.graph_reload_error}`);
            onSignedIn();
          } else if (r.status === "error") {
            setLoginError(r.error || "Sign-in failed.");
            clearLogin();
          } else pollDevice(flowId, intervalMs);
        } catch (exc) {
          setLoginError(errMsg(exc));
          clearLogin();
        }
      }, intervalMs);
    },
    [onSignedIn, clearLogin],
  );

  const startSignIn = useCallback(
    async (provider: string) => {
      setLoginBusy(true);
      setLoginError("");
      clearLogin();
      try {
        const r = await api.oauthStart(provider);
        setLogin({
          provider,
          mode: r.mode,
          flowId: r.flow_id,
          userCode: r.user_code,
          verifyUri: r.verification_uri,
          authorizeUrl: r.authorize_url,
        });
        const url = r.mode === "device" ? r.verification_uri : r.authorize_url;
        if (url) window.open(url, "_blank", "noopener,noreferrer");
        if (r.mode === "device") pollDevice(r.flow_id, (r.interval ?? 5) * 1000);
      } catch (exc) {
        setLoginError(errMsg(exc));
      } finally {
        setLoginBusy(false);
      }
    },
    [clearLogin, pollDevice],
  );

  const completeSignIn = useCallback(async () => {
    if (!login) return;
    setLoginBusy(true);
    setLoginError("");
    try {
      const r = await api.oauthComplete(login.flowId, loginCode.trim());
      if (r.status === "complete") {
        if (r.graph_reload_error) setLoginError(`Signed in, but the agent did not restart cleanly: ${r.graph_reload_error}`);
        onSignedIn();
      } else setLoginError(r.error || "Sign-in failed.");
    } catch (exc) {
      setLoginError(errMsg(exc));
    } finally {
      setLoginBusy(false);
    }
  }, [login, loginCode, onSignedIn]);

  const disconnect = useCallback(
    async (provider: string) => {
      setLoginBusy(true);
      setLoginError("");
      try {
        await api.oauthDisconnect(provider);
        // Disconnect unloads the live graph (#2459) — converge composer/status.
        void queryClient.invalidateQueries();
        await refreshStatus();
        cbRef.current?.onDisconnected?.();
      } catch (exc) {
        setLoginError(errMsg(exc));
      } finally {
        setLoginBusy(false);
      }
    },
    [queryClient, refreshStatus],
  );

  return {
    status,
    refreshStatus,
    login,
    loginCode,
    setLoginCode,
    loginBusy,
    loginError,
    startSignIn,
    completeSignIn,
    cancelSignIn,
    disconnect,
  };
}

/** The standard account card (Settings ▸ Model): status line + sign-in /
 *  disconnect actions + the in-flight device/redirect flow. The wizard renders
 *  its own JSX over the same hook (it interleaves model probing). */
export function OAuthAccountCard({ provider }: { provider: string }) {
  const lc = useOauthLifecycle();
  const { status, refreshStatus } = lc;
  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const st = status[provider];
  const label = OAUTH_PROVIDER_LABEL[provider] || provider;
  const shortLabel = label.replace(" subscription", "");

  return (
    <div data-testid="oauth-account-card">
      {st?.signed_in ? (
        <Callout tone="success">
          <ShieldCheck size={15} /> Signed in — {st.detail || "credentials found"}.{" "}
          <Button type="button" onClick={() => void refreshStatus()}>
            Re-check
          </Button>{" "}
          <Button
            type="button"
            onClick={() => void lc.disconnect(provider)}
            disabled={lc.loginBusy}
            title="Signs this agent out and disables chat until you reconnect. protoAgent's stored credential is removed; a login shared with your CLI is never revoked remotely."
          >
            {lc.loginBusy ? <Spinner size={15} /> : null} Disconnect
          </Button>
        </Callout>
      ) : lc.login && lc.login.provider === provider ? (
        <Callout tone="warning">
          {lc.login.mode === "device" ? (
            <>
              <KeyRound size={15} /> In the tab that opened, enter code <code>{lc.login.userCode}</code> at{" "}
              <a href={lc.login.verifyUri} target="_blank" rel="noreferrer">
                {lc.login.verifyUri}
              </a>
              . <Spinner size={13} /> Waiting for approval…{" "}
              <Button type="button" onClick={lc.cancelSignIn}>
                Cancel
              </Button>
            </>
          ) : (
            <div className="setup-grid model-row">
              <FormField label="Approve in the opened tab, then paste the code shown">
                <Input
                  value={lc.loginCode}
                  onChange={(event) => lc.setLoginCode(event.target.value)}
                  placeholder="paste the code (looks like abc123#xyz)"
                />
              </FormField>
              <Button type="button" onClick={() => void lc.completeSignIn()} disabled={lc.loginBusy || !lc.loginCode.trim()}>
                {lc.loginBusy ? <Spinner size={15} /> : <ShieldCheck size={15} />}
                Complete sign-in
              </Button>
              <Button type="button" onClick={lc.cancelSignIn}>
                Cancel
              </Button>
            </div>
          )}
        </Callout>
      ) : (
        <Callout tone="warning">
          <KeyRound size={15} /> {st?.hint || "Not signed in."}{" "}
          <Button type="button" onClick={() => void lc.startSignIn(provider)} disabled={lc.loginBusy}>
            {lc.loginBusy ? <Spinner size={15} /> : <ShieldCheck size={15} />}
            Sign in with {shortLabel}
          </Button>{" "}
          <Button type="button" onClick={() => void refreshStatus()}>
            Re-check
          </Button>
        </Callout>
      )}
      {lc.loginError ? <Alert status="error">{lc.loginError}</Alert> : null}
    </div>
  );
}
