import { Spinner } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Copy, Network, QrCode, RefreshCw, Smartphone, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { StatusPill } from "../app/StatusPill";
import { formatCountdown, hostKindLabel, secondsLeft, tailnetFirst, type PairKind } from "../lib/agentPairing";
import { api } from "../lib/api";
import { readKey, removeKey, writeKey, writeKeyStrict } from "../lib/storage";
import { SettingsSubPanel } from "./SettingsSubPanel";
import "./devices.css";

import type { PairAddress, PairedDevice, PairHost } from "../lib/api";

type Device = PairedDevice;
/** A live code. `kind` is the dialog it belongs to: a phone QR (`device`) or a typeable agent
 *  code (`agent`, ADR 0113) — the same state machine, reachability step and claim-poll serve
 *  both, so the loopback fix below exists exactly once. */
type Pairing = { kind: PairKind; code: string; expires_at: number; ttl: number; hosts: PairHost[]; name?: string };
/** Loopback-bound: what we COULD bind to, so the panel can offer the fix rather than
 *  dead-ending on an error the operator has no way to act on. */
type Unreachable = { error: string; available: PairAddress[]; authConfigured: boolean };

function ago(ts: number | null): string {
  if (!ts) return "never used";
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return "active now";
  if (secs < 3600) return `last seen ${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `last seen ${Math.floor(secs / 3600)}h ago`;
  return `last seen ${Math.floor(secs / 86400)}d ago`;
}

/**
 * Settings ▸ Devices — paired devices and the QR that adds one (ADR 0087), plus "Pair an
 * agent" (ADR 0113): a typeable code another agent's hub redeems to join this agent as a
 * remote fleet member. The hub is just another paired client, so it lands in this same list
 * (badged "Agent") and is revoked the same way.
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
  const [starting, setStarting] = useState<PairKind | null>(null);
  // The code that ran out, so the panel can offer "New code" instead of silently vanishing —
  // an agent code is typed on ANOTHER machine, and the operator may only find out it expired
  // when the hub says "invalid or expired".
  const [expired, setExpired] = useState<PairKind | null>(null);
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
      const left = secondsLeft(pairing.expires_at);
      setRemaining(left);
      if (left <= 0) {
        setPairing(null);
        setExpired(pairing.kind);
      }
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
        setPairing(null); // claimed — the code is spent
        if (pairing.kind === "agent") {
          // The newest agent row is the hub that just claimed — name it, since the operator
          // is looking at THIS machine while the success happened on another one.
          const hub = [...res.devices].reverse().find((d) => d.kind === "agent");
          toast({
            tone: "success",
            title: "Agent paired",
            message: `${hub?.name ?? "The other agent's hub"} can now reach this agent. Remove it here to revoke.`,
          });
        } else {
          toast({ tone: "success", title: "Device paired", message: "It can now reach this agent." });
        }
      }
    }, 2000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairing]);

  // A live code is cancelled when the panel goes away too (Settings closed, section changed)
  // — not only via the Cancel button — so an abandoned agent code doesn't stay claimable for
  // the rest of its 5 minutes. A ref, because the unmount cleanup sees the FIRST render's
  // closure otherwise.
  const liveRef = useRef<PairKind | null>(null);
  liveRef.current = pairing?.kind ?? null;
  useEffect(
    () => () => {
      if (liveRef.current) void api.pairingCancel(liveRef.current).catch(() => {});
    },
    [],
  );

  async function startPairing(kind: PairKind) {
    setPairError(null);
    setUnreachable(null);
    setExpired(null);
    // Switching dialogs drops the other kind's code rather than leaving it claimable unseen.
    if (pairing && pairing.kind !== kind) {
      setPairing(null);
      void api.pairingCancel(pairing.kind).catch(() => {});
    }
    setStarting(kind);
    try {
      const res = await api.pairingStart(kind);
      if (res.ok) {
        setPairing({
          kind,
          code: res.code,
          expires_at: res.expires_at,
          ttl: res.ttl,
          // Tailnet first: it works from any network the other side is on (the server already
          // sorts this way; sorting again keeps the promise if it ever stops).
          hosts: tailnetFirst(res.hosts),
          name: res.name,
        });
        setHostIdx(0);
      } else if (res.available.length) {
        // Loopback-bound but fixable — offer the addresses instead of an error the operator
        // can't act on. This is the desktop app's default state.
        setUnreachable({ error: res.error, available: res.available, authConfigured: res.authConfigured });
      } else {
        setPairError(`${res.error} No tailnet or LAN address was found on this machine either.`);
      }
    } catch (err) {
      setPairError(err instanceof Error ? err.message : "could not start pairing");
    } finally {
      setStarting(null);
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
    // Mint based on what the SERVER reports, never on what this browser happens to hold.
    // Those are different facts and they diverge the moment a token is rotated or removed —
    // and acting on the wrong one wrote a non-loopback bind onto an instance with no token,
    // which the boot guard then refuses, bricking the app until someone edits YAML by hand.
    const serverHasToken = unreachable?.authConfigured === true;
    try {
      if (!serverHasToken) {
        // The server REFUSES a non-loopback bind with no token, so exposing without one
        // isn't an option we could offer even if we wanted to.
        const token = crypto.randomUUID().replace(/-/g, "") + crypto.randomUUID().replace(/-/g, "");
        // Store locally BEFORE saving: `auth.token` applies live, so the very next request
        // would 401 this session if the browser weren't already holding it.
        // Strict (ADR 0114 D1): if the browser can't keep the token, throw into the catch
        // below BEFORE the server is told about a token this session wouldn't be sending.
        const prior = readKey("local", "protoagent.authToken");
        writeKeyStrict("local", "protoagent.authToken", token);
        const res = await api.saveSettings({ "auth.token": token }, "agent");
        if (!res.ok) {
          // Restore what was there, don't blank it — this browser may hold a token that is
          // still valid for something, and the save we just attempted never took effect.
          if (prior) writeKey("local", "protoagent.authToken", prior);
          else removeKey("local", "protoagent.authToken");
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
    const kind = pairing?.kind;
    setPairing(null);
    setExpired(null);
    // Scoped to this dialog's kind: an unscoped cancel would also drop a code of the other
    // kind (ADR 0113 — both kinds share the pending store).
    if (kind) await api.pairingCancel(kind).catch(() => {});
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
          <>
            <Button
              type="button"
              variant="ghost"
              onClick={() => startPairing("agent")}
              loading={starting === "agent"}
              disabled={starting != null}
            >
              <Network size={15} aria-hidden /> {starting === "agent" ? "Preparing…" : "Pair an agent"}
            </Button>
            <Button
              type="button"
              onClick={() => startPairing("device")}
              loading={starting === "device"}
              disabled={starting != null}
            >
              <QrCode size={15} aria-hidden /> {starting === "device" ? "Preparing…" : "Add a device"}
            </Button>
          </>
        )
      }
    >
      <p className="setting-desc">
        Phones, tablets and other agents&apos; hubs paired to this agent. Each holds its own
        token, so removing one here doesn&apos;t sign out anything else.
      </p>

      {pairError && <p className="setting-desc devices-error">{pairError}</p>}

      {needsRestart && (
        <section className="devices-notice" aria-label="Restart required">
          <p className="devices-pair-hint">
            <strong>Restart protoAgent to finish.</strong> The bind interface only takes effect
            at startup — reopen the app, then add your device or pair the other agent.
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
            what keeps this app working while your phone or another agent connects — and will
            require a token; one is generated now if you don&apos;t have one. Pick the address
            you&apos;ll share.
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

      {expired && !pairing && (
        <section className="devices-notice" aria-label="Pairing code expired">
          <p className="devices-pair-hint">
            The {expired === "agent" ? "agent code" : "QR code"} expired before it was used.
          </p>
          <div className="devices-hosts">
            <Button type="button" onClick={() => startPairing(expired)} loading={starting === expired}>
              <RefreshCw size={14} aria-hidden /> New code
            </Button>
            <Button type="button" variant="ghost" onClick={() => setExpired(null)}>
              Dismiss
            </Button>
          </div>
        </section>
      )}

      {pairing?.kind === "agent" && (
        // An agent code is READ off this screen and TYPED on another machine's hub (ADR 0113
        // D2), so it gets the room a QR gets for phones: big, monospace, copyable. Every
        // reachable base URL is listed (tailnet first) because the hub needs one of them if
        // its discovery didn't already find this agent.
        <section className="devices-pair devices-pair--agent" aria-label="Pair another agent">
          <div className="devices-pair-body">
            <p className="devices-pair-hint">
              Enter this code on the other agent&apos;s hub. Expires in{" "}
              <strong>{formatCountdown(remaining)}</strong>.
            </p>
            <div className="devices-code">
              <code data-testid="agent-pair-code" aria-label={`Pairing code ${pairing.code.split("").join(" ")}`}>
                {pairing.code}
              </code>
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  void navigator.clipboard.writeText(pairing.code);
                  toast({ title: "Code copied", message: "Paste it into Pair… on the other hub." });
                }}
              >
                <Copy size={14} aria-hidden /> Copy
              </Button>
            </div>
            {pairing.hosts.length > 0 && (
              <>
                <p className="setting-desc">
                  {pairing.name ? <strong>{pairing.name}</strong> : "This agent"} is reachable at:
                </p>
                <ul className="devices-agent-hosts">
                  {pairing.hosts.map((h) => (
                    <li key={h.host}>
                      <Badge status={h.kind === "tailnet" ? "info" : "neutral"}>{hostKindLabel(h.kind)}</Badge>
                      <code>{h.url}</code>
                    </li>
                  ))}
                </ul>
              </>
            )}
            <p className="setting-desc">
              On the other agent&apos;s hub: <strong>Settings ▸ Agents ▸ Pair…</strong>, or{" "}
              <code>protoagent fleet pair &lt;url&gt; &lt;code&gt;</code>.
            </p>
          </div>
        </section>
      )}

      {pairing?.kind !== "agent" && pairing && host && (
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
              Scan with the device&apos;s camera. Expires in <strong>{formatCountdown(remaining)}</strong>.
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
          description="Add a phone or tablet, or pair another agent's hub, to reach this agent without typing a token."
        />
      ) : (
        <div className="subagent-list devices-list">
          {devices.map((d) => (
            <div className="subagent-row" key={d.id}>
              <div>
                {/* Text-first like the other manager rows — an inline glyph here sits off
                    the strong's baseline and earns nothing; the panel is already "Devices". */}
                <strong>
                  {d.name}
                  {/* Which entries are other agents (ADR 0113 D3) — the kind is fixed by the
                      code the client claimed, so this can't be spoofed by the claimer. */}
                  <Badge status={d.kind === "agent" ? "info" : "neutral"}>
                    {d.kind === "agent" ? "Agent" : "Device"}
                  </Badge>
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
