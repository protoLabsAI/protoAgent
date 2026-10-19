import { Spinner } from "@protolabsai/ui/data";
import { Input } from "@protolabsai/ui/forms";
import { useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Pencil, ShieldCheck, ShieldMinus, Smartphone, Trash2 } from "lucide-react";
import { useState } from "react";

import { StatusPill } from "../app/StatusPill";
import { api } from "../lib/api";
import { notifyAuthRequired } from "../lib/auth";
import { currentDeviceId, forgetDeviceCredential } from "../lib/credentials";
import "./device-list.css";

import type { DeviceScope, PairedDevice } from "../lib/api";

/** Matches the server's `ACTIVE_WINDOW_SECONDS` (last-seen throttle 300 s + 60 s margin) —
 *  used only when an older server's rows carry no `active` field. */
export const ACTIVE_WINDOW_SECONDS = 360;
/** Matches `security.devices.MAX_NAME_LEN`; the server is authoritative. */
export const MAX_DEVICE_NAME = 64;

export function isActive(d: Pick<PairedDevice, "active" | "last_seen_at">, nowSecs = Date.now() / 1000): boolean {
  if (typeof d.active === "boolean") return d.active;
  return !!d.last_seen_at && nowSecs - d.last_seen_at < ACTIVE_WINDOW_SECONDS;
}

/** The row's subtitle. Last-seen is stamped at most every 5 minutes (a disk write per stamp),
 *  so for an active device "last seen 3m ago" would understate it — say what we know. */
export function lastSeenLabel(d: Pick<PairedDevice, "active" | "last_seen_at">, nowSecs = Date.now() / 1000): string {
  const ts = d.last_seen_at;
  if (!ts) return "never used";
  if (isActive(d, nowSecs)) return "in use in the last few minutes";
  const secs = Math.max(0, Math.floor(nowSecs - ts));
  if (secs < 3600) return `last seen ${Math.max(1, Math.floor(secs / 60))}m ago`;
  if (secs < 86400) return `last seen ${Math.floor(secs / 3600)}h ago`;
  return `last seen ${Math.floor(secs / 86400)}d ago`;
}

/** Idle-expiry copy for a row (ADR 0121 D5), or null when it never expires (full access, an
 *  agent, `devices.idle_ttl_days: 0`, or a server before S7.3). An expired device stays
 *  listed until it's removed; it can't be revived from here — the phone pairs again. */
export function expiryLabel(
  d: Pick<PairedDevice, "expired" | "expires_at">,
  nowSecs = Date.now() / 1000,
): string | null {
  if (d.expired) return "Expired after going unused — remove it, then pair the phone again";
  if (d.expires_at == null) return null;
  const days = Math.ceil((d.expires_at - nowSecs) / 86400);
  if (days <= 1) return "expires within a day if unused";
  return `expires in ${days} days if unused`;
}

/** The scope badge for a row (ADR 0121 D4), or null when there is none to show: an agent is
 *  always full access (its "Agent" badge says enough), and a pre-ADR-0121 server sends no
 *  scope. */
export function scopeBadge(d: Pick<PairedDevice, "kind" | "scope">): { label: string; status: "warning" | "neutral" } | null {
  if (d.kind === "agent" || !d.scope) return null;
  return d.scope === "device" ? { label: "Limited", status: "warning" } : { label: "Full access", status: "neutral" };
}

/** The scope action a row offers (Limit a full-access phone, Promote a limited one), or null.
 *  None when THIS console's credential is itself limited — the server would refuse it, and a
 *  button that can only fail is a dead one. */
export function scopeAction(
  d: Pick<PairedDevice, "kind" | "scope">,
  callerLimited: boolean,
): { label: "Limit" | "Promote"; to: DeviceScope } | null {
  if (callerLimited || d.kind === "agent" || !d.scope) return null;
  return d.scope === "full" ? { label: "Limit", to: "device" } : { label: "Promote", to: "full" };
}

/** Whether a limited console may rename/remove this row: only its own (ADR 0121 D4). */
export function canManage(rowId: string, selfId: string, callerLimited: boolean): boolean {
  return !callerLimited || (!!selfId && rowId === selfId);
}

/**
 * The paired-devices list in Settings ▸ Devices (ADR 0087, #4336 S3): rename in place, the
 * row for THIS browser marked "This device", and removing yourself confirmed in-page (the
 * console's dialogs never use `confirm()`) before it signs this browser out.
 *
 * Split out of `DevicesPanel` so the list and the pairing dialogs can change independently.
 */
export function DeviceList({
  devices,
  loading,
  onChanged,
  callerLimited = false,
  callerDeviceId = null,
}: {
  devices: PairedDevice[];
  loading: boolean;
  onChanged: () => Promise<void> | void;
  /** This console's own credential is device scope (ADR 0121 D4). */
  callerLimited?: boolean;
  /** The server's answer to "which device is asking" — a fallback for the localStorage id. */
  callerDeviceId?: string | null;
}) {
  const toast = useToast();
  const [revoking, setRevoking] = useState<string | null>(null);
  const [confirmSelf, setConfirmSelf] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  // Read per render — it's a localStorage read, and it must reflect a pairing made in this
  // tab without a reload.
  const thisId = currentDeviceId() || callerDeviceId || "";
  const [scoping, setScoping] = useState<string | null>(null);

  async function changeScope(d: PairedDevice, to: DeviceScope) {
    setScoping(d.id);
    try {
      await api.setDeviceScope(d.id, to);
      toast(
        to === "full"
          ? { title: `${d.name} has full access`, message: "It can now pair devices, manage plugins and change credentials." }
          : { title: `${d.name} is limited`, message: "It can't pair devices, manage plugins or change credentials any more." },
      );
      await onChanged();
    } catch (err) {
      // A device-scope refusal arrives already readable (lib/api/http.ts).
      toast({ tone: "error", title: `Couldn't change ${d.name}`, message: err instanceof Error ? err.message : "Try again." });
    } finally {
      setScoping(null);
    }
  }

  function startRename(d: PairedDevice) {
    setEditing(d.id);
    setDraft(d.name);
    setRenameError(null);
    setConfirmSelf(null);
  }

  function cancelRename() {
    setEditing(null);
    setRenameError(null);
  }

  async function saveRename(d: PairedDevice) {
    const name = draft.trim();
    if (!name) {
      setRenameError("Give it a name.");
      return;
    }
    if (name === d.name) {
      cancelRename();
      return;
    }
    setSaving(true);
    setRenameError(null);
    try {
      await api.renameDevice(d.id, name);
      setEditing(null);
      await onChanged();
    } catch (err) {
      setRenameError(err instanceof Error ? err.message : "could not rename the device");
    } finally {
      setSaving(false);
    }
  }

  async function revoke(d: PairedDevice) {
    const self = d.id === thisId;
    setRevoking(d.id);
    try {
      await api.revokeDevice(d.id);
      if (self) {
        // This browser's own token just stopped working. Forget it rather than letting the
        // next request 401 into a confusing gate; an operator token kept here (a desktop
        // browser that also paired itself) keeps the console signed in.
        setConfirmSelf(null);
        if (forgetDeviceCredential()) {
          toast({ title: "Removed this device", message: "Still connected with this browser's operator token." });
          await onChanged();
        } else {
          toast({ title: "Removed this device", message: "You're signed out here. Pair again or enter a token to reconnect." });
          notifyAuthRequired();
        }
        return;
      }
      toast({ title: `Removed ${d.name}`, message: "Its token no longer works." });
      await onChanged();
    } catch (err) {
      toast({ tone: "error", title: `Couldn't remove ${d.name}`, message: err instanceof Error ? err.message : "Try again." });
    } finally {
      setRevoking(null);
    }
  }

  if (loading) {
    return (
      <div className="devices-loading">
        <Spinner size={18} />
        <span>Loading devices…</span>
      </div>
    );
  }
  if (devices.length === 0) {
    return (
      <Empty
        icon={<Smartphone size={20} />}
        title="No paired devices"
        description="Add a phone or tablet, or pair another agent's hub, to reach this agent without typing a token."
      />
    );
  }

  return (
    <div className="subagent-list devices-list">
      {devices.map((d) => {
        const self = !!thisId && d.id === thisId;
        const isEditing = editing === d.id;
        const badge = scopeBadge(d);
        const action = scopeAction(d, callerLimited);
        const manage = canManage(d.id, thisId, callerLimited);
        const expiry = expiryLabel(d);
        return (
          <div className={`subagent-row${self ? " devices-row--self" : ""}`} key={d.id} data-testid={`device-row-${d.id}`}>
            <div className="devices-row-main">
              {isEditing ? (
                <form
                  className="devices-rename"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void saveRename(d);
                  }}
                >
                  <Input
                    autoFocus
                    aria-label={`New name for ${d.name}`}
                    value={draft}
                    maxLength={MAX_DEVICE_NAME}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") {
                        // Cancel the edit only — not the whole Settings overlay.
                        e.stopPropagation();
                        cancelRename();
                      }
                    }}
                  />
                  <Button type="submit" loading={saving} disabled={saving}>
                    Save
                  </Button>
                  <Button type="button" variant="ghost" onClick={cancelRename} disabled={saving}>
                    Cancel
                  </Button>
                  {renameError && (
                    <p className="devices-rename-error" role="alert">
                      {renameError}
                    </p>
                  )}
                </form>
              ) : (
                // Text-first like the other manager rows — an inline glyph here sits off the
                // strong's baseline and earns nothing; the panel is already "Devices".
                <strong>
                  {d.name}
                  {/* Which entries are other agents (ADR 0113 D3) — the kind is fixed by the
                      code the client claimed, so this can't be spoofed by the claimer. */}
                  <Badge status={d.kind === "agent" ? "info" : "neutral"}>{d.kind === "agent" ? "Agent" : "Device"}</Badge>
                  {badge ? (
                    <Badge status={badge.status} data-testid={`device-scope-${d.id}`}>
                      {badge.label}
                    </Badge>
                  ) : null}
                  {d.expired ? <Badge status="error">Expired</Badge> : null}
                  {self ? <Badge status="success">This device</Badge> : null}
                  {isActive(d) ? <StatusPill label="active" tone="success" /> : null}
                </strong>
              )}
              <span>
                {lastSeenLabel(d)}
                {expiry ? <span data-testid={`device-expiry-${d.id}`}> · {expiry}</span> : null}
              </span>
              {confirmSelf === d.id && (
                <div className="devices-confirm" role="group" aria-label="Remove this device">
                  <p className="devices-pair-hint">
                    This is the device you&apos;re using. Removing it signs you out here.
                  </p>
                  <div className="devices-hosts">
                    <Button
                      type="button"
                      onClick={() => void revoke(d)}
                      loading={revoking === d.id}
                      disabled={revoking != null}
                    >
                      Remove and sign out
                    </Button>
                    <Button type="button" variant="ghost" onClick={() => setConfirmSelf(null)} disabled={revoking != null}>
                      Keep
                    </Button>
                  </div>
                </div>
              )}
            </div>
            {!isEditing && (action || manage) && (
              <div className="issue-actions">
                {action && (
                  <Button
                    variant="ghost"
                    type="button"
                    title={action.label === "Limit" ? "Limit what this device can change" : "Give this device full access"}
                    aria-label={`${action.label} ${d.name}`}
                    loading={scoping === d.id}
                    disabled={scoping != null || revoking != null}
                    onClick={() => void changeScope(d, action.to)}
                  >
                    {action.label === "Limit" ? <ShieldMinus size={15} aria-hidden /> : <ShieldCheck size={15} aria-hidden />}{" "}
                    {action.label}
                  </Button>
                )}
                {manage && (
                <>
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  title="Rename"
                  aria-label={`Rename ${d.name}`}
                  disabled={revoking != null}
                  onClick={() => startRename(d)}
                >
                  <Pencil size={15} />
                </Button>
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  title="Remove"
                  aria-label={`Remove ${d.name}`}
                  loading={revoking === d.id && !self}
                  disabled={revoking != null}
                  onClick={() => {
                    if (self) {
                      setEditing(null);
                      setConfirmSelf(d.id);
                    } else {
                      void revoke(d);
                    }
                  }}
                >
                  <Trash2 size={15} />
                </Button>
                </>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
