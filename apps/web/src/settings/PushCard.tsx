import { Badge, Button } from "@protolabsai/ui/primitives";
import { AlertTriangle, Bell, BellOff, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { isDesktopWebview } from "../lib/api/desktop";
import { deviceToken, otherDeviceAddresses } from "../lib/credentials";
import { consoleUrl } from "../lib/deviceHandoff";
import { enrollPush, PushError, pushState, pushStatus, renewPush, unenrollPush, type PushState } from "../lib/push";

type RenewReason = "vapid_key" | "handoff" | "idle";
/** What the card shows. The first three come from where the console runs and never change
 *  while it is open; the rest come from the browser and this device's status on the agent. */
type View =
  | { kind: "loading" | "desktop" | "insecure" | "ios" | "unsupported" | "shared" | "denied" | "off" | "trouble" }
  | { kind: "renew"; reason: RenewReason }
  | { kind: "on"; renewedAt: number | null };
type Focus = "heading" | "action";

export const PUSH_COPY = {
  turningOn: "Turning on…",
  renewing: "Renewing…",
  on: "Notifications are on for this device.",
  off: "Notifications are off for this device.",
  trouble: "Couldn't check notifications on this device.",
  denied: "Notifications are blocked in this browser's settings.",
  unsupported: "This browser can't get notifications.",
  shared: "Pair this browser to get notifications.",
} as const;

const RENEW_REASON: Record<string, string> = {
  vapid_key: "This agent changed its notification key.",
  handoff: "This phone moved to a new address, which needs its own notifications.",
  idle: "This device went unused for too long.",
};

/** What the agent answers when it refuses to turn on or renew, in the operator's words. */
const REFUSAL: Record<string, string> = {
  endpoint_in_use: "Another paired device already uses this browser's notifications. Turn them off there first.",
  not_enrollable: "This device can't change notifications right now. Reload the console and try again.",
  invalid_endpoint: "This browser's push service isn't supported yet.",
};

const PROVIDER: Record<string, string> = { apple: "Apple", google: "Google", mozilla: "Mozilla" };

const DISCLOSURE = [
  "Alerts go straight from this machine to your browser's push service: Apple, Google or Mozilla. No protoAgent server sits in between, and you don't need a chat app.",
  'The alert says only "protoAgent needs your attention" and "Open the console to view a decision" (or "an update"). It never includes the question, chat text, job output, names or file paths.',
  "The push service can see when alerts are sent, how big they are and this machine's public IP address.",
  "Opening an alert asks you to sign in to the console. Nothing can be approved from the lock screen.",
];

function isApple(): boolean {
  return /iPhone|iPad|iPod/.test(navigator.userAgent ?? "") ||
    (navigator.platform === "MacIntel" && (navigator.maxTouchPoints ?? 0) > 1);
}

/** Where the console runs, when that alone decides the card (states 1 to 3). */
function environment(): "desktop" | "insecure" | "ios" | null {
  if (typeof window === "undefined") return null;
  // The desktop app is not a paired device: its shell shows every notice itself (ADR 0135 W2d).
  if (isDesktopWebview()) return "desktop";
  if (window.isSecureContext !== true) return "insecure";
  const installed = window.matchMedia?.("(display-mode: standalone)").matches === true ||
    (navigator as { standalone?: boolean }).standalone === true;
  return isApple() && !installed ? "ios" : null;
}

/** Where a blocked browser keeps the switch. Only the browser can undo a block. */
function deniedHint(): string {
  if (isApple()) return "On iPhone, open Settings, then Notifications, then protoAgent, and allow notifications.";
  if (/Android/.test(navigator.userAgent ?? "")) return "Tap the icon beside the address, then Permissions, and allow notifications.";
  return "Click the icon beside the address, then allow notifications for this site.";
}

function notADevice(err: unknown): boolean {
  return err instanceof PushError && err.status === 403 && err.code === "not_a_device";
}

/** "Renewed 2 days ago" from an epoch-seconds stamp. */
export function renewedAgo(at: number, now: number = Date.now()): string {
  const s = Math.max(0, now / 1000 - at);
  const unit = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"} ago`;
  if (s < 60) return "just now";
  if (s < 3600) return unit(Math.floor(s / 60), "minute");
  if (s < 86400) return unit(Math.floor(s / 3600), "hour");
  return unit(Math.floor(s / 86400), "day");
}

/** States 4 to 10, first match wins. A failed read is "couldn't check", never "off": an
 *  enrolled device must not be offered "Turn on" because one request failed. */
async function readView(): Promise<View> {
  try {
    const state = await pushState();
    if (state === "unsupported") return { kind: "unsupported" };
    // A shared sign-in holds no device token, so there is no device to ask about.
    if (!deviceToken()) return { kind: "shared" };
    const status = await pushStatus();
    if (state === "denied") return { kind: "denied" };
    if (status.renew) return { kind: "renew", reason: status.renew };
    return state === "enrolled" ? { kind: "on", renewedAt: status.binding?.renewed_at ?? null } : { kind: "off" };
  } catch (err) {
    return notADevice(err) ? { kind: "shared" } : { kind: "trouble" };
  }
}

/** The one sentence that says where a re-check landed. */
function sentence(view: View): string {
  switch (view.kind) {
    case "on": return PUSH_COPY.on;
    case "off": return PUSH_COPY.off;
    case "denied": return PUSH_COPY.denied;
    case "unsupported": return PUSH_COPY.unsupported;
    case "shared": return PUSH_COPY.shared;
    case "renew": return RENEW_REASON[view.reason] ?? "";
    case "trouble": return PUSH_COPY.trouble;
    default: return "";
  }
}

/** How long the status line stays empty before the next sentence lands. A screen reader
 *  skips a live region whose text didn't change, so a repeat needs the gap. */
export const ANNOUNCE_GAP_MS = 20;

export type PushCardProps = {
  /** `push.provider` of this device's row in the device list; unknown values show nothing. */
  provider?: string | null;
  /** Open the pairing flow (the shared sign-in state). */
  onPair?: () => void;
  /** This device's enrollment changed: the device list is worth reading again. */
  onChanged?: () => void;
};

/** Settings ▸ Devices ▸ "Notifications on this device" (ADR 0135 D10 / W2e): turn this
 *  device's alerts on, renew them or turn them off, and see what an alert reveals. The turn-on
 *  handler calls `enrollPush` first thing: iOS only grants permission from the tap itself. */
export function PushCard({ provider, onPair, onChanged }: PushCardProps) {
  const [env] = useState(environment);
  const [view, setView] = useState<View>(env ? { kind: env } : { kind: "loading" });
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const headingId = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const actions = useRef<HTMLDivElement>(null);
  const status = useRef<HTMLParagraphElement>(null);
  const announceTimer = useRef<number | null>(null);
  const focusNext = useRef<Focus | null>(null);
  // An action always wins over a background read. Starting one bumps the ticket, which
  // retires any read in flight, and no read starts while one runs: the permission prompt
  // closing fires `focus` in the middle of enrolling, and that read must not discard it.
  const ticket = useRef(0);
  const acting = useRef(false);

  /** Put one sentence in the status line. Cleared first, so a repeat is spoken again. */
  const say = useCallback((text: string) => {
    const el = status.current;
    if (announceTimer.current !== null) window.clearTimeout(announceTimer.current);
    announceTimer.current = null;
    if (!el) return;
    el.textContent = "";
    if (!text) return;
    announceTimer.current = window.setTimeout(() => {
      announceTimer.current = null;
      el.textContent = text;
    }, ANNOUNCE_GAP_MS);
  }, []);

  const refresh = useCallback(() => {
    if (env || acting.current) return;
    const mine = ++ticket.current;
    void readView().then((next) => { if (ticket.current === mine) setView(next); });
  }, [env]);

  useEffect(() => {
    refresh();
    // Coming back from the browser's or the system's settings: permission may have changed.
    const onReturn = () => { if (document.visibilityState === "visible") refresh(); };
    document.addEventListener("visibilitychange", onReturn);
    window.addEventListener("focus", onReturn);
    return () => {
      ticket.current++;
      if (announceTimer.current !== null) window.clearTimeout(announceTimer.current);
      document.removeEventListener("visibilitychange", onReturn);
      window.removeEventListener("focus", onReturn);
    };
  }, [refresh]);

  // Focus moves once the action's result is on screen: a loading button is disabled, and a
  // disabled button can't hold focus.
  useEffect(() => {
    if (busy || !focusNext.current) return;
    const target = focusNext.current === "heading" ? heading.current : actions.current?.querySelector("button");
    focusNext.current = null;
    (target ?? heading.current)?.focus();
  });

  const begin = (announce: string) => {
    acting.current = true;
    ticket.current++;
    setBusy(true);
    setRefusal(null);
    say(announce);
  };
  const end = (next: View | null, focus: Focus, announce = "", refused: string | null = null) => {
    acting.current = false;
    focusNext.current = focus;
    if (next) setView(next);
    setRefusal(refused);
    setBusy(false);
    say(announce);
  };
  const trouble = () => end({ kind: "trouble" }, "heading", PUSH_COPY.trouble);

  async function settleEnroll(enrolling: Promise<PushState>) {
    let state: PushState;
    try {
      state = await enrolling;
    } catch (err) {
      const code = err instanceof PushError ? err.code : null;
      if (code && REFUSAL[code]) return end(null, "action", REFUSAL[code], REFUSAL[code]);
      if (code !== "stale_vapid_version") return trouble();
      // The agent's key moved between the read and the write. `renewPush` reads it again
      // and never prompts; a second refusal is not something another tap will fix.
      try {
        state = await renewPush();
      } catch {
        return trouble();
      }
    }
    if (state === "enrolled") {
      const fresh = await pushStatus().catch(() => null);
      end({ kind: "on", renewedAt: fresh?.binding?.renewed_at ?? null }, "heading", PUSH_COPY.on);
      onChanged?.();
    } else if (state === "denied") end({ kind: "denied" }, "heading");
    else if (state === "unsupported") end({ kind: "unsupported" }, "heading");
    // The permission prompt was dismissed: nothing changed, so nothing to say.
    else end(null, "action");
  }

  const turnOn = () => {
    const enrolling = enrollPush();
    begin(view.kind === "renew" ? PUSH_COPY.renewing : PUSH_COPY.turningOn);
    void settleEnroll(enrolling);
  };
  const turnOff = () => {
    begin("");
    unenrollPush().then(
      () => {
        end({ kind: "off" }, "action", PUSH_COPY.off);
        onChanged?.();
      },
      // The browser's own subscription may be gone even though the agent wasn't told, so
      // this is "couldn't check", and "Try again" reads what is true now.
      trouble,
    );
  };
  const tryAgain = () => {
    begin("");
    void readView().then((next) =>
      next.kind === "trouble" ? end(null, "action", PUSH_COPY.trouble) : end(next, "heading", sentence(next)),
    );
  };

  const httpsOrigin = view.kind === "insecure" ? otherDeviceAddresses().find((a) => a.origin.startsWith("https://"))?.origin : undefined;
  const service = provider ? PROVIDER[provider] : undefined;
  const canEnroll = view.kind === "off" || view.kind === "renew";

  return (
    <section className="devices-push" data-testid="push-card" data-state={view.kind} aria-labelledby={headingId}>
      <h3 id={headingId} ref={heading} tabIndex={-1} className="devices-push-heading">
        Notifications on this device
      </h3>
      {view.kind !== "loading" && (
        <div className="devices-push-state" data-testid="push-state">
          {view.kind === "desktop" && (
            <>
              <strong>Desktop app</strong>
              <p>The desktop app shows alerts while it&apos;s running, including when its window is closed. Quitting the app stops them.</p>
            </>
          )}
          {view.kind === "insecure" && (
            <>
              <strong>Not available on this address</strong>
              <p>Notifications need an HTTPS address.</p>
            </>
          )}
          {view.kind === "ios" && (
            <>
              <strong>Add to Home Screen first</strong>
              <p>On iPhone, notifications work only from the Home Screen app.</p>
              <ol>
                <li>Tap Share, then Add to Home Screen.</li>
                <li>Open protoAgent from your Home Screen.</li>
                <li>If it asks, pair this phone again there.</li>
                <li>Tap Turn on notifications.</li>
              </ol>
              <p className="devices-push-footnote">Needs iOS 16.4 or later.</p>
            </>
          )}
          {view.kind === "unsupported" && (
            <>
              <strong>Not supported</strong>
              <p>{PUSH_COPY.unsupported}</p>
            </>
          )}
          {view.kind === "shared" && (
            <>
              <strong>Shared sign-in</strong>
              <p>{PUSH_COPY.shared}</p>
            </>
          )}
          {view.kind === "denied" && (
            <>
              <strong>Blocked</strong>
              <p>{PUSH_COPY.denied}</p>
              <p className="devices-push-footnote">{deniedHint()}</p>
            </>
          )}
          {view.kind === "renew" && (
            <>
              <strong>Renew on this device</strong>
              {RENEW_REASON[view.reason] ? <p>{RENEW_REASON[view.reason]}</p> : null}
            </>
          )}
          {view.kind === "off" && (
            <>
              <strong>Off on this device</strong>
              <p>Get alerts on this device even when the console is closed.</p>
            </>
          )}
          {view.kind === "on" && (
            <>
              <span><Badge status="success">On</Badge></span>
              {service ? <p>Push service: {service}</p> : null}
              {view.renewedAt ? <p>Renewed {renewedAgo(view.renewedAt)}</p> : null}
            </>
          )}
          {view.kind === "trouble" && <p>{PUSH_COPY.trouble}</p>}
        </div>
      )}
      <div className="devices-push-actions" ref={actions}>
        {httpsOrigin ? <a href={consoleUrl(httpsOrigin)}>Open this agent&apos;s HTTPS address</a> : null}
        {view.kind === "shared" && onPair ? (
          <Button type="button" onClick={onPair}>Pair this device</Button>
        ) : null}
        {canEnroll ? (
          <Button type="button" variant="primary" onClick={turnOn} loading={busy}>
            {view.kind === "renew" ? <RefreshCw size={15} aria-hidden /> : <Bell size={15} aria-hidden />}
            {view.kind === "renew" ? "Renew notifications" : "Turn on notifications"}
          </Button>
        ) : null}
        {view.kind === "on" ? (
          <Button type="button" variant="ghost" onClick={turnOff} loading={busy}>
            <BellOff size={15} aria-hidden /> Turn off on this device
          </Button>
        ) : null}
        {view.kind === "trouble" ? (
          <Button type="button" variant="ghost" onClick={tryAgain} loading={busy}>Try again</Button>
        ) : null}
      </div>
      {refusal && canEnroll ? (
        <p className="devices-push-refusal" data-testid="push-refusal">
          <AlertTriangle size={15} aria-hidden /> <span>{refusal}</span>
        </p>
      ) : null}
      {/* The only live region. Its text is written by `say`, never by React. */}
      <p className="devices-push-status" role="status" data-testid="push-status" ref={status} />
      <details className="devices-push-disclosure">
        <summary>What leaves this machine</summary>
        {DISCLOSURE.map((line) => <p key={line}>{line}</p>)}
      </details>
    </section>
  );
}
