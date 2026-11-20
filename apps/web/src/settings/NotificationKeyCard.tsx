import { Button } from "@protolabsai/ui/primitives";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { api, ApiError, type PushKeys, type PushKeyVersion } from "../lib/api";

export const KEY_COPY = {
  fullAccessOnly: "Only full-access sign-ins can manage the notification key.",
  loadFailed: "Couldn't load the notification key.",
  rotateFailed: "Couldn't rotate the notification key. Try again.",
  none: "No notification key yet. This agent creates one when a device first turns on notifications.",
} as const;

type View =
  | { kind: "loading" | "trouble" | "forbidden" }
  | { kind: "ready"; data: PushKeys };

const forbidden = (err: unknown) => err instanceof ApiError && err.status === 403;

/** "18 Dec" from an epoch-seconds stamp, in the reader's own time zone. */
export function retireDay(at: number): string {
  return new Date(at * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** The migration window in whole days, as the confirm says it. */
export function windowDays(seconds: number): string {
  const days = Math.max(0, Math.floor(seconds / 86400));
  return `${days} day${days === 1 ? "" : "s"}`;
}

/** One line per version that has been rotated out and still has a retirement moment. */
function retiringLine(key: PushKeyVersion, count: number, now: number): string {
  const at = key.retire_after as number;
  const line = `Version ${key.version} ${at > now ? "retires" : "retired"} on ${retireDay(at)}.`;
  if (count <= 0) return line;
  return `${line} ${count} ${count === 1 ? "device still uses" : "devices still use"} it.`;
}

/** Settings ▸ Devices ▸ "Notification key" (ADR 0135): which signing-key version is active,
 *  how many devices still sit on a version that is on its way out, and "Rotate key…". Shows
 *  versions and dates only: never a key, public or private. There is deliberately no control
 *  here for retiring a compromised key.
 *
 *  `fullAccess` is the `caller` of `GET /api/devices`: a device-scope sign-in gets no card
 *  and makes no request, since the server refuses it the rotation anyway. */
export function NotificationKeyCard({ fullAccess }: { fullAccess: boolean }) {
  return fullAccess ? <KeyCard /> : null;
}

function KeyCard() {
  const [view, setView] = useState<View>({ kind: "loading" });
  const [confirming, setConfirming] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const headingId = useId();
  const actions = useRef<HTMLDivElement>(null);
  const confirm = useRef<HTMLDivElement>(null);
  // Set when the confirm closes, so focus goes back to "Rotate key…" once it is on screen again.
  const refocus = useRef(false);
  // Only the newest read lands: a slow first load must not overwrite the post-rotate one.
  const ticket = useRef(0);
  const alive = useRef(true);

  const load = useCallback(async () => {
    const mine = ++ticket.current;
    let next: View;
    try {
      next = { kind: "ready", data: await api.pushKeys() };
    } catch (err) {
      next = { kind: forbidden(err) ? "forbidden" : "trouble" };
    }
    if (alive.current && mine === ticket.current) setView(next);
  }, []);

  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);

  // Opening lands on Cancel, the safe answer; closing hands focus back to the opener.
  useEffect(() => {
    if (confirming) {
      const buttons = confirm.current?.querySelectorAll<HTMLButtonElement>("button");
      buttons?.[buttons.length - 1]?.focus();
    } else if (refocus.current) {
      refocus.current = false;
      actions.current?.querySelector<HTMLButtonElement>("button")?.focus();
    }
  }, [confirming]);

  const close = () => {
    refocus.current = true;
    setConfirming(false);
  };

  const rotate = async () => {
    setRotating(true);
    setError(null);
    try {
      const fresh = await api.rotatePushKey();
      if (!alive.current) return;
      setSaid(`Key version ${fresh.version} is now active.`);
      await load();
      if (!alive.current) return;
      close();
    } catch (err) {
      if (!alive.current) return;
      if (forbidden(err)) {
        // Not this sign-in's to do: the sentence replaces the button for good.
        ticket.current++;
        setView({ kind: "forbidden" });
        setConfirming(false);
      } else {
        setError(KEY_COPY.rotateFailed);
      }
    } finally {
      if (alive.current) setRotating(false);
    }
  };

  const data = view.kind === "ready" ? view.data : null;
  const keys = data?.keys ?? [];
  const active = keys.find((k) => k.active);
  const retiring = keys.filter((k) => !k.active && typeof k.retire_after === "number");
  const now = Date.now() / 1000;

  return (
    <section className="devices-push" data-testid="notification-key-card" data-state={view.kind} aria-labelledby={headingId}>
      <h3 id={headingId} className="devices-push-heading">
        Notification key
      </h3>
      {view.kind === "forbidden" && (
        <p className="devices-pair-hint" data-testid="notification-key-forbidden">
          {KEY_COPY.fullAccessOnly}
        </p>
      )}
      {view.kind === "trouble" && (
        <>
          <p className="devices-pair-hint">{KEY_COPY.loadFailed}</p>
          <div className="devices-push-actions">
            <Button type="button" variant="ghost" onClick={() => void load()}>
              Try again
            </Button>
          </div>
        </>
      )}
      {data && (
        <>
          <div className="devices-push-state">
            {active ? <strong>Key version {active.version} · active</strong> : <p>{KEY_COPY.none}</p>}
            {retiring.map((k) => (
              <p key={k.version} data-testid={`notification-key-retiring-${k.version}`}>
                {retiringLine(k, data.bindings_by_version?.[String(k.version)] ?? 0, now)}
              </p>
            ))}
          </div>
          {active && !confirming && (
            <div className="devices-push-actions" ref={actions}>
              <Button type="button" variant="ghost" onClick={() => { setError(null); setConfirming(true); }}>
                Rotate key…
              </Button>
            </div>
          )}
          {active && confirming && (
            <div
              className="devices-confirm"
              role="group"
              aria-label="Confirm key rotation"
              ref={confirm}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  // Cancel the rotation only — not the whole Settings overlay.
                  e.stopPropagation();
                  if (!rotating) close();
                }
              }}
            >
              <p className="devices-pair-hint">
                Every device must renew within {windowDays(data.rotation_window_seconds)}. Devices that don&apos;t
                renew stop getting alerts.
              </p>
              <div className="devices-hosts">
                <Button type="button" variant="danger" onClick={() => void rotate()} loading={rotating} disabled={rotating}>
                  Rotate key
                </Button>
                <Button type="button" variant="ghost" onClick={close} disabled={rotating}>
                  Cancel
                </Button>
              </div>
              {error && (
                <p className="devices-rename-error" role="alert">
                  {error}
                </p>
              )}
            </div>
          )}
        </>
      )}
      <p className="devices-push-status" role="status">
        {said}
      </p>
    </section>
  );
}
