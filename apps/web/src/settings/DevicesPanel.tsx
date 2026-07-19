import { Spinner } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { Button, Empty } from "@protolabsai/ui/primitives";
import { Copy, QrCode, Smartphone, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { StatusPill } from "../app/StatusPill";
import { api } from "../lib/api";
import { SettingsSubPanel } from "./SettingsSubPanel";
import "./devices.css";

import type { PairAddress, PairHost } from "../lib/api";

type Device = { id: string; name: string; created_at: number; last_seen_at: number | null };
type Pairing = { code: string; expires_at: number; ttl: number; hosts: PairHost[] };
/** Loopback-bound: what we COULD bind to, so the panel can offer the fix rather than
 *  dead-ending on an error the operator has no way to act on. */
type Unreachable = { error: string; available: PairAddress[] };

function ago(ts: number | null): string {
  if (!ts) return "never used";
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return "active now";
  if (secs < 3600) return `last seen ${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `last seen ${Math.floor(secs / 3600)}h ago`;
  return `last seen ${Math.floor(secs / 86400)}d ago`;
}

/**
 * Settings ▸ Devices — paired devices and the QR that adds one (ADR 0087).
 *
 * Wrapped in `SettingsSubPanel` like every other hand-built panel (Keyboard, Delegates) so
 * the header/padding/scroll treatment comes from one container and can't drift per panel,
 * and rows reuse `.subagent-list`/`.subagent-row`, the list shape the other managers use.
 * Only the pairing card is bespoke, because nothing else in Settings looks like it.
 *
 * The QR arrives rendered from the server: doing it here would mean a QR library in the
 * console AND the pairing URL assembled in two places, and fetching it from a `GET …?code=`
 * endpoint would put the code in access logs — the leak the fragment design avoids.
 */
export function DevicesPanel() {
  const toast = useToast();
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [pairError, setPairError] = useState<string | null>(null);
  const [unreachable, setUnreachable] = useState<Unreachable | null>(null);
  const [exposing, setExposing] = useState<string | null>(null);
  const [needsRestart, setNeedsRestart] = useState(false);
  // A token MINTED by this flow exists nowhere the operator can see it — it goes straight
  // into this browser's localStorage. Every other client (the desktop app's own webview, the
  // CLI, another browser) then gets 401s with no way to know the secret. Surface it once.
  const [mintedToken, setMintedToken] = useState<string | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [hostIdx, setHostIdx] = useState(0);
  // Every mutating action gets a visible pending state — a button that looks identical
  // before and after a click is indistinguishable from a dead one.
  const [starting, setStarting] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await api.devices();
      setDevices(res.devices || []);
    } catch {
      /* read-mostly panel; a transient failure just leaves the last list */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Countdown + expiry. The code dies server-side at TTL regardless; this keeps the UI
  // honest rather than showing a QR that silently stopped working.
  useEffect(() => {
    if (!pairing) return;
    const tick = () => {
      const left = Math.max(0, Math.round(pairing.expires_at - Date.now() / 1000));
      setRemaining(left);
      if (left <= 0) setPairing(null);
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [pairing]);

  // While a code is live, poll for the device that claims it — the phone can't tell this
  // window it succeeded, so the list growing IS the confirmation.
  useEffect(() => {
    if (!pairing) {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = null;
      return;
    }
    const before = devices.length;
    pollRef.current = window.setInterval(async () => {
      const res = await api.devices().catch(() => null);
      if (!res) return;
      if ((res.devices || []).length > before) {
        setDevices(res.devices);
        setPairing(null); // claimed — the QR is spent
        toast({ tone: "success", title: "Device paired", message: "It can now reach this agent." });
      }
    }, 2000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairing]);

  async function startPairing() {
    setPairError(null);
    setUnreachable(null);
    setStarting(true);
    try {
      const res = await api.pairingStart();
      if (res.ok) {
        setPairing({ code: res.code, expires_at: res.expires_at, ttl: res.ttl, hosts: res.hosts });
        setHostIdx(0);
      } else if (res.available.length) {
        // Loopback-bound but fixable — offer the addresses instead of an error the operator
        // can't act on. This is the desktop app's default state.
        setUnreachable({ error: res.error, available: res.available });
      } else {
        setPairError(`${res.error} No tailnet or LAN address was found on this machine either.`);
      }
    } catch (err) {
      setPairError(err instanceof Error ? err.message : "could not start pairing");
    } finally {
      setStarting(false);
    }
  }

  /**
   * Make the agent reachable so phones can pair, minting an auth token first if the instance
   * has none.
   *
   * Binds `0.0.0.0`, NOT the address the operator picked. uvicorn takes ONE host, and binding
   * a single non-loopback address DROPS loopback — which breaks the desktop app outright,
   * because its webview talks to its own sidecar over `http://127.0.0.1:<port>`. Choosing
   * "Tailnet" therefore selects the address we ADVERTISE in the QR, not the only one we
   * listen on. `0.0.0.0` is the only single value that satisfies both callers.
   *
   * ORDER MATTERS. `auth.token` applies LIVE (no restart), so writing it would 401 this very
   * session on the next request if the browser didn't already hold it. Store it locally
   * first, then save; roll the local value back if the save fails, or we'd lock ourselves
   * out with a token the server never accepted.
   *
   * `network.bind` is host-scoped and restart-gated, hence the restart notice rather than a
   * silent "done".
   */
  async function makeReachable(addr: PairAddress) {
    setExposing(addr.host);
    const previous = window.localStorage.getItem("protoagent.authToken");
    try {
      if (!previous) {
        // The server REFUSES a non-loopback bind with no token, so exposing without one
        // isn't an option we could offer even if we wanted to.
        const token = crypto.randomUUID().replace(/-/g, "") + crypto.randomUUID().replace(/-/g, "");
        window.localStorage.setItem("protoagent.authToken", token);
        const res = await api.saveSettings({ "auth.token": token }, "agent");
        if (!res.ok) {
          window.localStorage.removeItem("protoagent.authToken");
          throw new Error(res.messages.join(" · ") || "could not set an auth token");
        }
        setMintedToken(token);
      }
      // See the note above: 0.0.0.0, not addr.host — a specific bind kills loopback and with
      // it the desktop app's own connection to the sidecar.
      const bind = await api.saveSettings({ "network.bind": "0.0.0.0" }, "host");
      if (!bind.ok) throw new Error(bind.messages.join(" · ") || "could not set the bind address");
      setUnreachable(null);
      setNeedsRestart(true);
    } catch (err) {
      setPairError(err instanceof Error ? err.message : "could not update the bind address");
    } finally {
      setExposing(null);
    }
  }

  async function stopPairing() {
    setPairing(null);
    await api.pairingCancel().catch(() => {});
  }

  async function revoke(device: Device) {
    setRevoking(device.id);
    try {
      await api.revokeDevice(device.id);
      toast({ title: `Removed ${device.name}`, message: "Its token no longer works." });
      await refresh();
    } finally {
      setRevoking(null);
    }
  }

  const host = pairing?.hosts[hostIdx];

  return (
    <SettingsSubPanel
      label="devices"
      title="Devices"
      actions={
        pairing ? (
          <Button type="button" variant="ghost" onClick={stopPairing}>
            Cancel
          </Button>
        ) : (
          <Button type="button" onClick={startPairing} loading={starting}>
            <QrCode size={15} aria-hidden /> {starting ? "Preparing…" : "Add a device"}
          </Button>
        )
      }
    >
      <p className="setting-desc">
        Phones and tablets paired to this agent. Each holds its own token, so removing one
        here doesn&apos;t sign out anything else.
      </p>

      {pairError && <p className="setting-desc devices-error">{pairError}</p>}

      {needsRestart && (
        <section className="devices-notice" aria-label="Restart required">
          <p className="devices-pair-hint">
            <strong>Restart protoAgent to finish.</strong> The bind interface only takes effect
            at startup — reopen the app, then add your device.
          </p>
          {mintedToken && (
            <>
              <p className="setting-desc">
                This agent had no token, so one was created. <strong>Save it now</strong> — it
                isn&apos;t shown again. Anything else that talks to this agent (the desktop app
                after restart, the CLI, another browser) will ask for it.
              </p>
              <div className="devices-token">
                <code>{mintedToken}</code>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => {
                    void navigator.clipboard.writeText(mintedToken);
                    toast({ title: "Token copied", message: "Keep it somewhere safe." });
                  }}
                >
                  <Copy size={14} aria-hidden /> Copy
                </Button>
              </div>
            </>
          )}
        </section>
      )}

      {unreachable && (
        <section className="devices-notice" aria-label="Make this agent reachable">
          <p className="devices-pair-hint">{unreachable.error}</p>
          <p className="setting-desc">
            Allow devices on your network to reach it. This agent will start listening on{" "}
            <strong>all</strong> your network interfaces rather than localhost only — that&apos;s
            what keeps this app working while your phone connects — and will require a token; one
            is generated now if you don&apos;t have one. Pick the address to put in the QR below.
            Undo it any time in Settings ▸ Network by setting the bind interface back to{" "}
            <code>127.0.0.1</code>.
          </p>
          <div className="devices-hosts">
            {unreachable.available.map((a) => (
              <Button
                key={a.host}
                type="button"
                variant="ghost"
                loading={exposing === a.host}
                disabled={exposing != null}
                onClick={() => makeReachable(a)}
              >
                {a.kind === "tailnet" ? "Tailnet" : "Wi-Fi"} · {a.host}
              </Button>
            ))}
          </div>
          <p className="setting-desc">
            {unreachable.available.some((a) => a.kind === "tailnet")
              ? "Tailnet is the safer address to share — only your own devices can reach it, from any network."
              : "This is a local-network address, so it's reachable by anything on this Wi-Fi."}
          </p>
        </section>
      )}

      {pairing && host && (
        <section className="devices-pair" aria-label="Pair a new device">
          {host.qr ? (
            // Server-rendered SVG. Injected as markup because it IS the payload — same
            // origin, generated from a URL we just built ourselves.
            <div className="devices-qr" dangerouslySetInnerHTML={{ __html: host.qr }} />
          ) : (
            <p className="setting-desc">Scan unavailable — use the link below.</p>
          )}
          <div className="devices-pair-body">
            <p className="devices-pair-hint">
              Scan with the device&apos;s camera. Expires in <strong>{remaining}s</strong>.
            </p>
            {pairing.hosts.length > 1 && (
              <div className="devices-hosts">
                {pairing.hosts.map((h, i) => (
                  <button
                    key={h.host}
                    type="button"
                    className={`devices-host${i === hostIdx ? " on" : ""}`}
                    onClick={() => setHostIdx(i)}
                  >
                    {h.kind === "tailnet" ? "Tailnet" : "Wi-Fi"} · {h.host}
                  </button>
                ))}
              </div>
            )}
            <code className="devices-url">{host.url}</code>
            <p className="setting-desc">
              {host.kind === "tailnet"
                ? "Works from any network the device is on."
                : "Only works while both are on this Wi-Fi."}
            </p>
          </div>
        </section>
      )}

      {loading ? (
        <div className="devices-loading">
          <Spinner size={18} />
          <span>Loading devices…</span>
        </div>
      ) : devices.length === 0 ? (
        <Empty
          icon={<Smartphone size={20} />}
          title="No paired devices"
          description="Add a phone or tablet to reach this agent without typing a token."
        />
      ) : (
        <div className="subagent-list">
          {devices.map((d) => (
            <div className="subagent-row" key={d.id}>
              <div>
                {/* Text-first like the other manager rows — an inline glyph here sits off
                    the strong's baseline and earns nothing; the panel is already "Devices". */}
                <strong>
                  {d.name}
                  {d.last_seen_at && Date.now() / 1000 - d.last_seen_at < 60 ? (
                    <StatusPill label="active" tone="success" />
                  ) : null}
                </strong>
                <span>{ago(d.last_seen_at)}</span>
              </div>
              <div className="issue-actions">
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  title="Remove"
                  aria-label={`Remove ${d.name}`}
                  loading={revoking === d.id}
                  disabled={revoking != null}
                  onClick={() => revoke(d)}
                >
                  <Trash2 size={15} />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </SettingsSubPanel>
  );
}
