import { useEffect, useState } from "react";

/**
 * Cold-start boot gate (adapted from ORBIS's BootStatus). The desktop build
 * launches a PyInstaller-frozen sidecar that needs ~30s on first run to unpack
 * and compile the LangGraph agent; until the engine is up, every API call fails
 * with WKWebView's opaque "Load failed" / "Could not connect". Worse, the graph
 * compile runs inline on the server's event loop, so finishing the setup wizard
 * (or a model change) freezes the sidecar for the compile's duration — every
 * concurrent poller gets a connection refusal.
 *
 * Rather than flash those errors, we hold a full-screen gate until the engine
 * reports ready (the shell's runtime probe sees `graph_loaded`). The gate stays
 * down while the setup wizard should show (no graph expected yet), and
 * re-engages for the post-setup compile. An escape hatch ("Continue anyway")
 * appears after a grace period so a graph that never compiles (e.g. bad creds)
 * can't trap the operator on the loading screen — they can reach Settings.
 *
 * Purely visual, like ORBIS: it sits over the always-mounted app as a sibling
 * overlay and returns null once ready.
 */

const STUCK_AFTER_MS = 45_000; // offer "Continue anyway" past this

type BootGateProps = {
  /** True once the app should be shown — engine ready, or the wizard is due. */
  ready: boolean;
  /** True once the probe has exhausted its retries without ever reaching the engine. */
  failed: boolean;
  /** Agent display name (from identity.name), so the gate copy is white-labelled. */
  name: string;
  /** Re-arm the runtime probe (manual retry after a give-up). */
  onRetry: () => void;
  /** Dismiss the gate manually (escape hatch when the engine is slow to compile). */
  onContinue: () => void;
};

export function BootGate({ ready, failed, name, onRetry, onContinue }: BootGateProps) {
  const [stuck, setStuck] = useState(false);

  useEffect(() => {
    if (ready) return; // resolved before the grace period — no timer needed
    const t = window.setTimeout(() => setStuck(true), STUCK_AFTER_MS);
    return () => window.clearTimeout(t);
  }, [ready]);

  if (ready) return null;

  return (
    <div className="boot-gate" role="status" aria-live="polite">
      <div className="boot-gate-inner">
        <img
          src={`${import.meta.env.BASE_URL}protolabs-icon-outline.svg`}
          alt=""
          className="boot-gate-mark"
        />
        {failed ? (
          <>
            <div className="boot-gate-title">{name} isn’t responding</div>
            <p className="boot-gate-detail">
              The engine didn’t come up in time. It may still be warming up — give
              it another moment, then retry.
            </p>
            <button type="button" className="boot-gate-retry" onClick={onRetry}>
              Retry
            </button>
          </>
        ) : (
          <>
            <div className="boot-gate-spinner" aria-hidden="true" />
            <div className="boot-gate-title">Starting {name}…</div>
            <p className="boot-gate-detail">
              {stuck
                ? "This is taking longer than usual. The engine may still be compiling, or it may need attention in Settings."
                : "Warming up the engine — first launch (and finishing setup) can take up to a minute. Later launches are quick."}
            </p>
            {stuck ? (
              <button type="button" className="boot-gate-retry" onClick={onContinue}>
                Continue anyway
              </button>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
