import { useToast } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { Bell, BellOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { deviceToken } from "../lib/credentials";
import { enrollPush, pushState, unenrollPush, type PushState } from "../lib/push";

/** Why this console can't be asked yet, in the operator's words — or null when it can. */
export function pushBlocker(): string | null {
  if (typeof window === "undefined") return null;
  if (!deviceToken()) return "Notifications are set up per paired phone or browser. Pair this one with Add a device first.";
  if (window.isSecureContext !== true) return "Notifications need this console's https address. Open it there, then turn them on.";
  const apple = /iPhone|iPad|iPod/.test(navigator.userAgent ?? "") ||
    (navigator.platform === "MacIntel" && (navigator.maxTouchPoints ?? 0) > 1);
  const installed = window.matchMedia?.("(display-mode: standalone)").matches === true ||
    (navigator as { standalone?: boolean }).standalone === true;
  if (apple && !installed) {
    return "On iPhone and iPad, add this console to your Home Screen (Share, then Add to Home Screen), open it from there, then turn notifications on.";
  }
  return null;
}

const UNSUPPORTED = "This browser can't receive notifications while the console is closed.";
const UNKNOWN = "Couldn't check whether notifications are on for this device. The agent didn't answer.";
const DENIED = "Notifications are blocked for this console. Allow them in your browser or system settings, then come back here.";

/** The first W2e control (ADR 0135): this device turns its own closed-console notifications
 *  on or off. The click handler calls `enrollPush` first thing — iOS only grants permission
 *  from the tap itself. */
export function PushToggle() {
  const toast = useToast();
  // "unknown" = the agent couldn't be asked. It is not "not enrolled": an enrolled device
  // must not be offered "Turn on" just because one status read failed.
  const [state, setState] = useState<PushState | "unknown" | null>(null);
  const [busy, setBusy] = useState(false);
  const blocker = pushBlocker();
  // An action always wins over a read. Starting one bumps the ticket, which retires any
  // read already in flight, and no read starts while one runs: the permission prompt
  // closing fires `focus` in the middle of enrolling, and that read must not discard it.
  const ticket = useRef(0);
  const acting = useRef(false);
  const act = (running: boolean) => { acting.current = running; setBusy(running); if (running) ticket.current++; };

  const refresh = useCallback(() => {
    if (acting.current) return;
    const mine = ++ticket.current;
    pushState().then(
      (s) => { if (ticket.current === mine) setState(s); },
      () => { if (ticket.current === mine) setState("unknown"); },
    );
  }, []);

  useEffect(() => {
    refresh();
    // Coming back from the browser's or the system's settings: permission may have changed.
    const onReturn = () => { if (document.visibilityState === "visible") refresh(); };
    document.addEventListener("visibilitychange", onReturn);
    window.addEventListener("focus", onReturn);
    return () => {
      ticket.current++;
      document.removeEventListener("visibilitychange", onReturn);
      window.removeEventListener("focus", onReturn);
    };
  }, [refresh]);

  const turnOn = () => {
    act(true);
    enrollPush().then(
      (next) => {
        setState(next);
        if (next === "enrolled") toast({ tone: "success", title: "Notifications are on", message: "This device gets decisions and urgent notices, even with the console closed." });
      },
      () => toast({ tone: "error", title: "Couldn't turn on notifications", message: "The agent didn't answer. Check the connection and try again." }),
    ).finally(() => act(false));
  };
  const turnOff = () => {
    act(true);
    unenrollPush().then(
      () => { act(false); setState("not enrolled"); },
      () => {
        act(false);
        toast({ tone: "error", title: "Couldn't turn off notifications", message: "The agent didn't answer. Check the connection and try again." });
        // The browser's own subscription may be gone even though the agent wasn't told.
        refresh();
      },
    );
  };

  const on = state === "enrolled";
  const note = on ? "On for this device. Decisions and urgent notices arrive even with the console closed."
    : blocker ?? (state === "unsupported" ? UNSUPPORTED : state === "denied" ? DENIED
      : state === "unknown" ? UNKNOWN
      : "Get decisions and urgent notices on this device, even with the console closed.");
  const canAsk = !on && !blocker && state === "not enrolled";

  return (
    <section className="devices-push" data-testid="push-toggle" aria-label="Notifications on this device">
      <div className="devices-push-body">
        <strong>Notifications on this device</strong>
        <p className="devices-pair-hint" data-testid="push-note">{note}</p>
      </div>
      {on ? (
        <Button type="button" variant="ghost" onClick={turnOff} loading={busy} disabled={busy}>
          <BellOff size={15} aria-hidden /> Turn off
        </Button>
      ) : canAsk ? (
        <Button type="button" onClick={turnOn} loading={busy} disabled={busy}>
          <Bell size={15} aria-hidden /> Turn on notifications
        </Button>
      ) : state === "unknown" && !blocker ? (
        <Button type="button" variant="ghost" onClick={refresh}>
          Check again
        </Button>
      ) : null}
    </section>
  );
}
