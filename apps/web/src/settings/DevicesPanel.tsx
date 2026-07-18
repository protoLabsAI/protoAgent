import { Button } from "@protolabsai/ui/primitives";
import { Empty } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";
import { Smartphone, Trash2, QrCode } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../lib/api";
import "./devices.css";

type Device = { id: string; name: string; created_at: number; last_seen_at: number | null };
type PairHost = { host: string; kind: "tailnet" | "lan"; url: string; qr: string | null };
type Pairing = { code: string; expires_at: number; ttl: number; hosts: PairHost[] };

function ago(ts: number | null): string {
  if (!ts) return "never";
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

/**
 * Devices — paired devices and the QR that adds one (ADR 0087).
 *
 * The QR arrives rendered from the server. Doing it here would mean a QR library in the
 * console AND assembling the pairing URL in two places; more importantly, fetching it from
 * a `GET …?code=` endpoint would put the code in access logs, which is exactly what the
 * fragment design avoids.
 */
export function DevicesPanel() {
  const toast = useToast();
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [pairError, setPairError] = useState<string | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [hostIdx, setHostIdx] = useState(0);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await api.devices();
      setDevices(res.devices || []);
    } catch {
      /* the panel is read-mostly; a transient failure just leaves the last list */
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
  // window it succeeded, so the list arriving IS the confirmation.
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
    try {
      const res = await api.pairingStart();
      setPairing(res);
      setHostIdx(0);
    } catch (err) {
      // The common case is a loopback-bound instance — the server explains it; surface that
      // rather than a generic failure, because the fix is a restart flag.
      setPairError(err instanceof Error ? err.message : "could not start pairing");
    }
  }

  async function stopPairing() {
    setPairing(null);
    await api.pairingCancel().catch(() => {});
  }

  async function revoke(device: Device) {
    await api.revokeDevice(device.id);
    toast({ title: `Removed ${device.name}`, message: "Its token no longer works." });
    void refresh();
  }

  const host = pairing?.hosts[hostIdx];

  return (
    <div className="devices-panel">
      <header className="devices-head">
        <div>
          <h3>Devices</h3>
          <p className="devices-sub">
            Each paired device gets its own token, so removing one here doesn&apos;t sign out
            anything else.
          </p>
        </div>
        {!pairing && (
          <Button type="button" onClick={startPairing}>
            <QrCode size={15} aria-hidden /> Add a device
          </Button>
        )}
      </header>

      {pairError && <p className="devices-error">{pairError}</p>}

      {pairing && host && (
        <section className="devices-pair" aria-label="Pair a new device">
          {host.qr ? (
            // Server-rendered SVG. Injected as markup because it IS the payload; it never
            // leaves this origin and is generated from a URL we just built ourselves.
            <div className="devices-qr" dangerouslySetInnerHTML={{ __html: host.qr }} />
          ) : (
            <p className="devices-sub">Scan unavailable — use the link below.</p>
          )}
          <div className="devices-pair-body">
            <p className="devices-pair-hint">
              Scan with the device&apos;s camera. Expires in <strong>{remaining}s</strong>.
            </p>
            <code className="devices-url">{host.url}</code>
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
            <p className="devices-sub">
              {host.kind === "tailnet"
                ? "Works from any network the device is on."
                : "Only works while both are on this Wi-Fi."}
            </p>
            <Button type="button" variant="ghost" onClick={stopPairing}>
              Cancel
            </Button>
          </div>
        </section>
      )}

      {loading ? null : devices.length === 0 ? (
        <Empty
          icon={<Smartphone size={20} />}
          title="No paired devices"
          description="Add a phone or tablet to reach this agent without typing a token."
        />
      ) : (
        <ul className="devices-list">
          {devices.map((d) => (
            <li key={d.id} className="devices-row">
              <Smartphone size={16} aria-hidden />
              <div className="devices-row-main">
                <span className="devices-name">{d.name}</span>
                <span className="devices-meta">Last seen {ago(d.last_seen_at)}</span>
              </div>
              <Button
                icon
                variant="ghost"
                type="button"
                aria-label={`Remove ${d.name}`}
                onClick={() => revoke(d)}
              >
                <Trash2 size={15} />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
