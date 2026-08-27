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
  const updateRef = useRef<UpdateInfo | null>(null);
  const checkInFlight = useRef<Promise<void> | null>(null);
  const interactiveRequested = useRef(false);
  const interactivePending = useRef(false);
  const interactiveAuthoritative = useRef(false);
  const launchSettled = useRef<Promise<void>>(Promise.resolve());
  const lastRequestId = useRef(0);

  const present = useCallback((next: UpdateInfo, show: boolean) => {
    updateRef.current = next;
    setUpdate(next);
    setPhase("available");
    setPct(0);
    setError(null);
    if (show) setOpen(true);
  }, []);

  const clearUpdate = useCallback(() => {
    updateRef.current = null;
    setUpdate(null);
    setOpen(false);
    setPhase("available");
  }, []);

  // One frontend check at a time. Rust applies the same single-flight contract across
  // launch + command calls, while this layer also coalesces repeated tray events and the
  // periodic timer into one piece of UI feedback.
  const runCheck = useCallback(
    (interactive: boolean) => {
      if (interactive) {
        interactiveRequested.current = true;
        interactivePending.current = true;
      }
      if (checkInFlight.current) return checkInFlight.current;
      const task = (async () => {
        try {
          const next = await api.checkUpdate();
          const shouldReport = interactiveRequested.current;
          if (shouldReport) interactiveAuthoritative.current = true;
          if (next) {
            present(next, shouldReport);
          } else if (shouldReport) {
            clearUpdate();
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
          interactivePending.current = false;
          interactiveRequested.current = false;
          checkInFlight.current = null;
        }
      })();
      checkInFlight.current = task;
      return task;
    },
    [clearUpdate, present, toast],
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
    let resolveSettled: (() => void) | null = null;
    launchSettled.current = new Promise<void>((resolve) => {
      resolveSettled = resolve;
    });
    const settle = () => {
      resolveSettled?.();
      resolveSettled = null;
    };
    const poll = async (tries: number) => {
      if (cancelled) return;
      let r: Awaited<ReturnType<typeof api.launchUpdateResult>>;
      try {
        r = await api.launchUpdateResult();
      } catch {
        settle();
        return;
      }
      if (cancelled) return;
      if (r === null) {
        settle(); // older shell — release the normal timer cycle
        return;
      }
      if (!r.done) {
        // Check still in flight (it races the webview boot) — cheap re-read, bounded.
        if (tries < 20) {
          retry = window.setTimeout(() => poll(tries + 1), 1_000);
        } else {
          settle(); // bounded wait: periodic checks must eventually resume
        }
        return;
      }
      if (r.update) {
        // A current/available result from an interactive check is newer authority than
        // the immutable launch snapshot. An error is not: wait for an overlapping tray
        // check, then fall back to the launch update if that check could not answer.
        if (interactivePending.current) await checkInFlight.current;
        if (cancelled) return;
        if (!interactiveAuthoritative.current) present(r.update, true);
      }
      settle();
    };
    poll(0);
    return () => {
      cancelled = true;
      window.clearTimeout(retry);
      settle();
    };
    // Mount-only on purpose: the launch result is immutable once done.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, present]);

  // Subscribe BEFORE consuming the durable request. If a click lands between those
  // operations it arrives both ways; the monotonic id makes that harmless. Rust targets
  // the event and consume command to `main`, and the primary marker keeps secondary windows
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
      const pending = await api.consumeUpdateRequest();
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
      if (cancelled || updateRef.current) return;
      await launchSettled.current;
      if (cancelled || updateRef.current) return;
      await runCheck(false);
    };
    const first = window.setTimeout(run, FIRST_CHECK_MS);
    const timer = window.setInterval(run, CHECK_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [enabled, runCheck]);

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
        clearUpdate();
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
