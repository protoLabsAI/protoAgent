import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@protolabsai/ui/primitives";
import { Dialog, useToast } from "@protolabsai/ui/overlays";
import { api, isDesktopWebview } from "../lib/api";
import { isPrimaryDesktopWindow, listen } from "../lib/desktop";
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
const UPDATE_REQUEST_EVENT = "updater:check-requested";

type UpdateInfo = { version: string; current: string; notes: string };
type Phase = "available" | "downloading" | "error";

export function UpdateNotice() {
  const enabled = isDesktopWebview() && isPrimaryDesktopWindow();
  const toast = useToast();
  const [update, setUpdate] = useState<UpdateInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>("available");
  const [pct, setPct] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const checkInFlight = useRef<Promise<void> | null>(null);
  const interactiveRequested = useRef(false);
  const directCheckStarted = useRef(false);
  const lastRequestId = useRef(0);

  const present = useCallback((next: UpdateInfo, show: boolean) => {
    setUpdate(next);
    setPhase("available");
    setPct(0);
    setError(null);
    if (show) setOpen(true);
  }, []);

  // One frontend check at a time. Rust applies the same single-flight contract across
  // launch + command calls, while this layer also coalesces repeated tray events and the
  // periodic timer into one piece of UI feedback.
  const runCheck = useCallback(
    (interactive: boolean) => {
      if (interactive) interactiveRequested.current = true;
      if (checkInFlight.current) return checkInFlight.current;
      directCheckStarted.current = true;
      const task = (async () => {
        try {
          const next = await api.checkUpdate();
          const shouldReport = interactiveRequested.current;
          if (next) {
            present(next, shouldReport);
          } else if (shouldReport) {
            toast({ tone: "success", title: "You're up to date", message: "This is the latest version of protoAgent." });
          }
        } catch (e) {
          if (interactiveRequested.current) {
            toast({
              tone: "error",
              title: "Couldn't check for updates",
              message: e instanceof Error ? e.message : String(e),
            });
          }
        } finally {
          interactiveRequested.current = false;
          checkInFlight.current = null;
        }
      })();
      checkInFlight.current = task;
      return task;
    },
    [present, toast],
  );

  // Launch check (#2203): the Rust shell runs ONE update check in parallel with engine
  // startup and stores the outcome; we poll that stored result (a state read, no
  // network) from mount, so the prompt lands seconds after the window opens instead of
  // after engine boot + the 10s settle. A launch-found update auto-opens the modal —
  // that's the "you're about to sit through startup; update instead?" moment. Timer-
  // found updates keep today's pill-only, don't-interrupt behavior.
  useEffect(() => {
    if (!enabled) return;
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
      // A direct tray/periodic check is newer than this immutable launch snapshot.
      if (r.update && !directCheckStarted.current) present(r.update, true);
    };
    poll(0);
    return () => {
      cancelled = true;
      window.clearTimeout(retry);
    };
    // Mount-only on purpose: the launch result is immutable once done.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, present]);

  // Subscribe BEFORE pulling the durable request. If a click lands between those
  // operations it arrives both ways; the monotonic id makes that harmless. Rust targets
  // the event and pull command to `main`, and the primary marker keeps secondary windows
  // from running any ambient update UX at all.
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let unlisten = () => {};
    const handle = (requestId: number) => {
      if (!Number.isSafeInteger(requestId) || requestId <= lastRequestId.current) return;
      lastRequestId.current = requestId;
      void api.ackUpdateRequest(requestId);
      void runCheck(true);
    };
    void listen<number>(UPDATE_REQUEST_EVENT, handle, {
      target: { kind: "WebviewWindow", label: "main" },
    }).then(async (off) => {
      if (cancelled) {
        off();
        return;
      }
      unlisten = off;
      const pending = await api.takeUpdateRequest();
      if (!cancelled && pending !== null) handle(pending);
    });
    return () => {
      cancelled = true;
      unlisten();
    };
  }, [enabled, runCheck]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const run = async () => {
      if (cancelled || update) return;
      await runCheck(false);
    };
    const first = window.setTimeout(run, FIRST_CHECK_MS);
    const timer = window.setInterval(run, CHECK_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [enabled, runCheck, update]);

  if (!enabled || !update) return null;

  const install = async () => {
    setPhase("downloading");
    setError(null);
    setPct(0);
    try {
      let got = 0;
      let total = 0;
      const result = await api.installUpdate(update.version, (e) => {
        if (e.contentLength) total = e.contentLength;
        got += e.chunkLength;
        if (total > 0) setPct(Math.min(100, Math.round((got / total) * 100)));
      });
      // On success the Rust command relaunches the app — only a freshness outcome
      // returns. Never install a release other than the one the operator confirmed.
      if (result.status === "superseded") {
        present(result.update, true);
        toast({
          tone: "info",
          title: "A newer update is now available",
          message: `Review ${result.update.version} before updating.`,
        });
      } else {
        setUpdate(null);
        setOpen(false);
        setPhase("available");
        toast({
          tone: "success",
          title: "You're up to date",
          message: "The previously offered update is no longer available.",
        });
      }
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
