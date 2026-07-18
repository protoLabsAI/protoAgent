import { Spinner } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { Button, Empty } from "@protolabsai/ui/primitives";
import { QrCode, Smartphone, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { StatusPill } from "../app/StatusPill";
import { api } from "../lib/api";
import { SettingsSubPanel } from "./SettingsSubPanel";
import "./devices.css";

type Device = { id: string; name: string; created_at: number; last_seen_at: number | null };
type PairHost = { host: string; kind: "tailnet" | "lan"; url: string; qr: string | null };
type Pairing = { code: string; expires_at: number; ttl: number; hosts: PairHost[] };

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
    setStarting(true);
    try {
      const res = await api.pairingStart();
      setPairing(res);
      setHostIdx(0);
    } catch (err) {
      // Usually a loopback-bound instance — the server explains it and the fix is a restart
      // flag, so surface its message rather than a generic failure.
      setPairError(err instanceof Error ? err.message : "could not start pairing");
    } finally {
      setStarting(false);
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
