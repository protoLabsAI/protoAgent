import { Spinner } from "@protolabsai/ui/data";
import { Input } from "@protolabsai/ui/forms";
import { useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Pencil, Smartphone, Trash2 } from "lucide-react";
import { useState } from "react";

import { StatusPill } from "../app/StatusPill";
import { api } from "../lib/api";
import { notifyAuthRequired } from "../lib/auth";
import { currentDeviceId, forgetDeviceCredential } from "../lib/credentials";
import "./device-list.css";

import type { PairedDevice } from "../lib/api";

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
}: {
  devices: PairedDevice[];
  loading: boolean;
  onChanged: () => Promise<void> | void;
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
  const thisId = currentDeviceId();

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
        const self = d.id === thisId;
        const isEditing = editing === d.id;
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
                  {self ? <Badge status="success">This device</Badge> : null}
                  {isActive(d) ? <StatusPill label="active" tone="success" /> : null}
                </strong>
              )}
              <span>{lastSeenLabel(d)}</span>
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
            {!isEditing && (
              <div className="issue-actions">
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
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
