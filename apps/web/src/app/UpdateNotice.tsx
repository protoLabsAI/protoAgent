import { useEffect, useState } from "react";
import { Button } from "@protolabsai/ui/primitives";
import { Dialog } from "@protolabsai/ui/overlays";
import { api, isDesktopWebview } from "../lib/api";
import { Markdown } from "../chat/LazyMarkdown";

/**
 * In-app update notice for the desktop shell (Tauri). Seeds from the shell's LAUNCH
 * check (#2203 — run in parallel with engine startup, pulled via
 * `updater_launch_result` the moment this mounts, auto-opening the modal), then
 * periodically re-checks the signed `latest.json`; a newer build surfaces an ambient
 * pill — click it for a **full modal** with the release **changelog rendered as
 * markdown** + a one-click "Update & Restart". User-driven (we notify; you choose when
 * to apply — no silent background install). Silent in dev / browser / offline / when up
 * to date. The updater work runs in the Rust shell (`updater_check` /
 * `updater_install`); this is the UX. Mirrors the orbis `UpdateNotice` pattern.
 */

const FIRST_CHECK_MS = 10_000; // let the boot settle
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000; // re-check every 6h

type UpdateInfo = { version: string; current: string; notes: string };
type Phase = "available" | "downloading" | "error";

export function UpdateNotice() {
  const [update, setUpdate] = useState<UpdateInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>("available");
  const [pct, setPct] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Launch check (#2203): the Rust shell runs ONE update check in parallel with engine
  // startup and stores the outcome; we poll that stored result (a state read, no
  // network) from mount, so the prompt lands seconds after the window opens instead of
  // after engine boot + the 10s settle. A launch-found update auto-opens the modal —
  // that's the "you're about to sit through startup; update instead?" moment. Timer-
  // found updates keep today's pill-only, don't-interrupt behavior.
  useEffect(() => {
    if (!isDesktopWebview()) return;
    let cancelled = false;
    let retry: number | undefined;
    const poll = async (tries: number) => {
      if (cancelled) return;
      const r = await api.launchUpdateResult();
      if (cancelled) return;
      if (r === null) return; // not desktop / older shell — the timer cycle below covers it
      if (!r.done) {
        // Check still in flight (it races the webview boot) — cheap re-read, bounded.
        if (tries < 20) retry = window.setTimeout(() => poll(tries + 1), 1_000);
        return;
      }
      if (r.update) {
        setUpdate(r.update);
        setOpen(true);
      }
    };
    poll(0);
    return () => {
      cancelled = true;
      window.clearTimeout(retry);
    };
    // Mount-only on purpose: the launch result is immutable once done.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!isDesktopWebview()) return;
    let cancelled = false;
    const run = async () => {
      if (cancelled || update) return;
      const u = await api.checkUpdate();
      if (!cancelled && u) setUpdate(u);
    };
    const first = window.setTimeout(run, FIRST_CHECK_MS);
    const timer = window.setInterval(run, CHECK_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [update]);

  if (!update) return null;

  const install = async () => {
    setPhase("downloading");
    setError(null);
    setPct(0);
    try {
      let got = 0;
      let total = 0;
      await api.installUpdate((e) => {
        if (e.contentLength) total = e.contentLength;
        got += e.chunkLength;
        if (total > 0) setPct(Math.min(100, Math.round((got / total) * 100)));
      });
      // On success the Rust command relaunches the app — we won't reach here.
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const footer = (
    <>
      {phase !== "downloading" && (
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Later
        </Button>
      )}
      <Button variant="primary" size="sm" onClick={install} disabled={phase === "downloading"}>
        {phase === "downloading" ? "Updating…" : phase === "error" ? "Retry" : "Update & Restart"}
      </Button>
    </>
  );

  return (
    <>
      <button
        type="button"
        className="update-notice-pill"
        onClick={() => setOpen(true)}
        aria-label={`Update available: ${update.version}`}
      >
        <span className="update-notice-dot" />
        Update · {update.version}
      </button>

      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        width={680}
        title={
          <>
            Update available <span className="update-notice-ver">{update.version}</span>
            <span className="update-notice-cur"> · you have {update.current}</span>
          </>
        }
        footer={footer}
      >
        {update.notes ? (
          <div className="update-notice-notes markdown">
            <Markdown>{update.notes}</Markdown>
          </div>
        ) : (
          <p className="update-notice-empty">A newer version is ready (you have {update.current}).</p>
        )}

        {phase === "downloading" && (
          <div className="update-notice-progress">
            <div className="update-notice-bar">
              <div className="update-notice-fill" style={{ width: `${pct}%` }} />
            </div>
            <div className="update-notice-pct">Downloading… {pct}%</div>
          </div>
        )}

        {phase === "error" && error && <p className="update-notice-err">Update failed: {error}</p>}
      </Dialog>
    </>
  );
}
