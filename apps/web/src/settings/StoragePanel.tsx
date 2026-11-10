import "./storage.css";

import { useQuery } from "@tanstack/react-query";
import { StatusDot } from "@protolabsai/ui/data";
import { ConfirmDialog, Tooltip, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Callout } from "@protolabsai/ui/primitives";
import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { clearQuickChats } from "../app/paletteChatStore";
import {
  clearSavedChats,
  freeUpSpace,
  protectedChatIds,
  subscribeTranscriptChanges,
  subscribeTranscriptSaveFailing,
  transcriptAgent,
  transcriptSaveFailing,
  transcriptStoreIfOpen,
  useAnyChatStreaming,
} from "../chat/chat-store";
import { TRANSCRIPT_DB } from "../chat/transcriptStore";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import { storagePressure, subscribeStoragePressure } from "../lib/storage";
import { lastAutoClear, subscribeEvictions, type AutoClear } from "../lib/storageEviction";
import type { FleetAgent } from "../lib/types";
import { SettingsSubPanel } from "./SettingsSubPanel";
import {
  joinAnd,
  lastClearSentence,
  lastSavedLabel,
  madeRoomSentence,
  plural,
  recentAutoClear,
  size,
} from "./storageCopy";
import { readStorageUsage, type AgentUsage, type StorageUsage } from "./storageUsage";

// Settings ▸ Storage — what this browser keeps for this console (docs/design/settings-storage.md,
// approved 2026-11-09; ADR 0114 D6). Clearing here removes this browser's copy only, never the
// server's. Copy is the spec's, verbatim.

type Estimate = { usage: number; quota: number } | null;
type PersistState = "checking" | "granted" | "unasked" | "refused";
type Confirm =
  | { kind: "agent"; row: AgentUsage }
  | { kind: "gone" }
  | { kind: "quick" }
  | { kind: "free" };

/** Below this share of the browser's quota, "with plenty of room left". */
const ROOMY_SHARE = 0.8;

function storageManager(): StorageManager | null {
  try {
    return typeof navigator !== "undefined" && navigator.storage ? navigator.storage : null;
  } catch {
    return null;
  }
}

function secureContext(): boolean {
  try {
    return window.isSecureContext !== false;
  } catch {
    return true;
  }
}

/** The D6 `failing` signal: the localStorage latch or a failing IndexedDB save. */
function useSaving(): boolean {
  const local = useSyncExternalStore(subscribeStoragePressure, storagePressure, storagePressure);
  const idb = useSyncExternalStore(subscribeTranscriptSaveFailing, transcriptSaveFailing, transcriptSaveFailing);
  return local.state !== "failing" && !idb;
}

export function StoragePanel() {
  const toast = useToast();
  const here = transcriptAgent();
  const replying = useAnyChatStreaming();
  const saving = useSaving();
  const fleet = useQuery({ queryKey: queryKeys.fleet, queryFn: () => api.fleet(), retry: false });

  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [estimate, setEstimate] = useState<Estimate>(null);
  const [last, setLast] = useState<AutoClear | null>(() => lastAutoClear());
  const [persist, setPersist] = useState<PersistState>("checking");
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [busy, setBusy] = useState(false);
  const [announce, setAnnounce] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  /** After a clear: focus the Clear button at this position once the list re-renders. */
  const focusAfter = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      setUsage(await readStorageUsage({ store: transcriptStoreIfOpen(), here, protect: protectedChatIds() }));
    } catch (err) {
      console.error("[storage] reading usage failed", err);
    }
    setLast(lastAutoClear());
    const manager = storageManager();
    if (manager && typeof manager.estimate === "function") {
      try {
        const e = await manager.estimate();
        setEstimate(typeof e.usage === "number" && typeof e.quota === "number" && e.quota > 0 ? { usage: e.usage, quota: e.quota } : null);
      } catch {
        setEstimate(null);
      }
    }
  }, [here]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  // Another tab saved or cleared chats, automatic clearing ran, or a reply settled.
  useEffect(() => subscribeTranscriptChanges(() => void refresh()), [refresh]);
  useEffect(() => subscribeEvictions(() => void refresh()), [refresh]);
  useEffect(() => {
    if (!replying) void refresh();
  }, [replying, refresh]);

  useEffect(() => {
    const manager = storageManager();
    if (!manager || typeof manager.persisted !== "function") return;
    manager
      .persisted()
      .then((granted) => setPersist((now) => (granted ? "granted" : now === "checking" ? "unasked" : now)))
      .catch(() => setPersist("unasked"));
  }, []);

  // Moving into or out of `failing` while open is announced politely (spec §7).
  const wasSaving = useRef(saving);
  useEffect(() => {
    if (wasSaving.current === saving) return;
    wasSaving.current = saving;
    setAnnounce(saving ? "Chats are saving in this browser." : "New messages aren't being saved in this browser.");
  }, [saving]);

  // Names from the fleet list; "not in fleet" only when the list is complete (it has the host).
  const agents = fleet.data?.agents;
  const { names, machines, inFleet } = useMemo(() => {
    const names = new Map<string, string>();
    const machines = new Map<string, string>();
    const complete = Boolean(agents?.some((a) => a.host));
    for (const a of agents ?? []) {
      const slug = a.host ? "host" : a.id;
      names.set(slug, displayName(a));
      if (a.remote && a.machine) machines.set(slug, a.machine);
    }
    return { names, machines, inFleet: complete ? new Set(names.keys()) : null };
  }, [agents]);
  const nameOf = useCallback((slug: string) => names.get(slug) ?? slug, [names]);
  const isGone = useCallback((slug: string) => inFleet !== null && !inFleet.has(slug), [inFleet]);

  const rows = usage?.agents ?? [];
  const gone = rows.filter((r) => isGone(r.slug));
  const goneBytes = gone.reduce((n, r) => n + r.bytes, 0);
  const goneChats = gone.reduce((n, r) => n + r.chats, 0);
  const chatsBytes = rows.reduce((n, r) => n + r.bytes, 0) + (usage?.quick.bytes ?? 0);
  const totalBytes = chatsBytes + (usage?.prefsBytes ?? 0);
  const hereName = nameOf(here);

  // Restore focus after a row left the list: the next row's Clear, else the section heading.
  useEffect(() => {
    if (focusAfter.current === null || !usage) return;
    const at = focusAfter.current;
    focusAfter.current = null;
    const buttons = [...(rootRef.current?.querySelectorAll<HTMLButtonElement>("[data-storage-clear]:not(:disabled)") ?? [])];
    const target = buttons[Math.min(at, buttons.length - 1)];
    if (target) target.focus();
    else headingRef.current?.focus();
  }, [usage]);

  const clearButtonIndex = (button: EventTarget | null) => {
    const buttons = [...(rootRef.current?.querySelectorAll("[data-storage-clear]:not(:disabled)") ?? [])];
    const i = button instanceof Element ? buttons.indexOf(button) : -1;
    return i < 0 ? 0 : i;
  };
  const pendingIndex = useRef(0);
  const ask = (next: Confirm, from: EventTarget | null) => {
    pendingIndex.current = clearButtonIndex(from);
    setConfirm(next);
  };

  async function run(action: Confirm) {
    setConfirm(null);
    setBusy(true);
    try {
      if (action.kind === "agent") {
        const res = await clearSavedChats(action.row.slug);
        toast({ message: `Cleared ${nameOf(action.row.slug)}'s chats from this browser. ${size(res.bytes)} freed.` });
      } else if (action.kind === "gone") {
        let chats = 0;
        let freed = 0;
        for (const row of gone) {
          const res = await clearSavedChats(row.slug);
          chats += res.chats;
          freed += res.bytes;
        }
        toast({ message: `Cleared ${plural(chats, "chat", "chats")} from agents no longer in your fleet. ${size(freed)} freed.` });
      } else if (action.kind === "quick") {
        const res = await clearQuickChats();
        toast({ message: `Cleared quick chats from this browser. ${size(res.bytes)} freed.` });
      } else {
        const res = await freeUpSpace();
        if (res.chats > 0) {
          toast({
            message: res.saving
              ? `Made room: cleared ${plural(res.chats, "old chat", "old chats")} (${size(res.bytes)}). New messages are saving again.`
              : `Made room: cleared ${plural(res.chats, "old chat", "old chats")} (${size(res.bytes)}).`,
          });
        } else if (res.saving) {
          toast({ message: "New messages are saving again." });
        }
      }
    } catch (err) {
      console.error("[storage] clearing failed", err);
    } finally {
      setBusy(false);
      focusAfter.current = pendingIndex.current;
      await refresh();
    }
  }

  const secure = secureContext();
  const manager = storageManager();
  const canPersist = secure && typeof manager?.persist === "function";
  async function keepData() {
    if (!manager || typeof manager.persist !== "function") return;
    try {
      setPersist((await manager.persist()) ? "granted" : "refused");
    } catch {
      setPersist("refused");
    }
  }

  const tooltipId = useId();

  return (
    <SettingsSubPanel label="Storage" title="Storage" kicker="what this browser keeps for this console — saved on this device">
      <div className="storage-panel" ref={rootRef}>
        <div className="storage-sr" role="status" aria-live="polite">
          {announce}
        </div>

        {/* ── Status (§3.1) ── */}
        {saving ? (
          <div className="storage-status">
            <p className="storage-status__head">
              <StatusDot status="success" />
              Chats are saving in this browser.
            </p>
            <p className="storage-muted">
              {estimate
                ? `${size(estimate.usage)} used${estimate.usage / estimate.quota < ROOMY_SHARE ? ", with plenty of room left" : ""}.`
                : `About ${size(totalBytes)} saved.`}
              {last && recentAutoClear(last) ? ` ${madeRoomSentence(last)}` : ""}
            </p>
          </div>
        ) : (
          <Callout tone="warning">
            <div className="storage-status">
              <p className="storage-status__head">New messages aren't being saved in this browser.</p>
              <p className="storage-muted">
                Chats keep working, and the server keeps the last 24 hours of each one. Free up space to start saving again.
              </p>
              <div>
                <Button variant="primary" onClick={(e) => ask({ kind: "free" }, e.currentTarget)} loading={busy}>
                  Free up space
                </Button>
              </div>
            </div>
          </Callout>
        )}

        {/* ── What's using space (§3.2) ── */}
        <section className="storage-section" aria-labelledby={`${tooltipId}-use`}>
          <h3 className="storage-h" id={`${tooltipId}-use`} ref={headingRef} tabIndex={-1}>
            What's using space
          </h3>
          <p className="storage-muted">
            Clearing removes this browser's copy only. The server keeps the last 24 hours of each chat, and your other devices
            aren't affected.
          </p>
          {gone.length > 0 && saving ? (
            <div className="storage-gone">
              <p className="storage-muted">
                {plural(gone.length, "agent is", "agents are")} no longer in your fleet ({size(goneBytes)}).
              </p>
              <Button size="sm" onClick={(e) => ask({ kind: "gone" }, e.currentTarget)} disabled={busy}>
                Clear them
              </Button>
            </div>
          ) : null}
          {usage ? (
          <ul className="storage-list">
            {rows.map((row) => {
              const name = nameOf(row.slug);
              const blocked = row.slug === here && replying;
              const descId = `${tooltipId}-${row.slug}`;
              const meta = `${plural(row.chats, "chat", "chats")} · ${size(row.bytes)}`;
              const saved = row.lastSavedAt ? lastSavedLabel(row.lastSavedAt) : "";
              return (
                <li className="storage-row" key={row.slug} data-slug={row.slug}>
                  <div className="storage-row__name">
                    <span className="storage-row__label">{name}</span>
                    {machines.get(row.slug) ? <small>{machines.get(row.slug)}</small> : null}
                    {row.slug === here ? <Badge>this window</Badge> : null}
                    {isGone(row.slug) ? <Badge>not in fleet</Badge> : null}
                  </div>
                  <div className="storage-row__end">
                    <Tooltip label={saved}>
                      <span className="storage-row__meta" tabIndex={saved ? 0 : undefined} aria-describedby={saved ? descId : undefined}>
                        {meta}
                      </span>
                    </Tooltip>
                    {saved ? (
                      <span id={descId} hidden>
                        {saved}
                      </span>
                    ) : null}
                    <Button
                      variant="ghost"
                      size="sm"
                      data-storage-clear=""
                      aria-label={`Clear ${name}'s chats`}
                      disabled={blocked || busy}
                      onClick={(e) => ask({ kind: "agent", row }, e.currentTarget)}
                    >
                      Clear
                    </Button>
                  </div>
                  {blocked ? <div className="storage-row__note">Replying now. You can clear this after the reply finishes.</div> : null}
                </li>
              );
            })}
            {usage?.quick.threads ? (
              <li className="storage-row" data-slug="quick">
                <div className="storage-row__name">
                  <span className="storage-row__label">Quick chats</span>
                </div>
                <div className="storage-row__end">
                  <span className="storage-row__meta">
                    {plural(usage.quick.threads, "thread", "threads")} · {size(usage.quick.bytes)}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    data-storage-clear=""
                    aria-label="Clear quick chats"
                    disabled={busy}
                    onClick={(e) => ask({ kind: "quick" }, e.currentTarget)}
                  >
                    Clear
                  </Button>
                </div>
              </li>
            ) : null}
            <li className="storage-row" data-slug="prefs">
              <div className="storage-row__name">
                <span className="storage-row__label">Layout and preferences</span>
              </div>
              <div className="storage-row__end">
                <span className="storage-row__meta">{size(usage.prefsBytes)}</span>
              </div>
              <div className="storage-row__note">Includes your sign-in, so it isn't cleared here.</div>
            </li>
          </ul>
          ) : null}
        </section>

        {/* ── Keep data (§3.4) ── */}
        <section className="setting-row storage-keep" aria-labelledby={`${tooltipId}-keep`}>
          <div className="setting-meta">
            <h3 className="storage-h" id={`${tooltipId}-keep`}>
              Keep data
            </h3>
            <p className="setting-desc">{keepDescription(secure, persist)}</p>
          </div>
          {persist === "granted" ? (
            <span className="storage-on">
              <StatusDot status="success" />
              On
            </span>
          ) : (
            <Button size="sm" disabled={!canPersist || persist === "checking"} onClick={() => void keepData()}>
              Keep data
            </Button>
          )}
        </section>

        {/* ── Last automatic clear (§3.5) ── */}
        <section className="storage-section" aria-labelledby={`${tooltipId}-last`}>
          <h3 className="storage-h" id={`${tooltipId}-last`}>
            Last automatic clear
          </h3>
          <p className="storage-muted">{last ? lastClearSentence(last, nameOf) : "Nothing has been cleared automatically."}</p>
        </section>

        {/* ── Technical details (§3.6) ── */}
        <details className="storage-details">
          <summary>Technical details</summary>
          <ul>
            <li>
              Browser's estimate:{" "}
              {estimate
                ? `${size(estimate.usage)} used of about ${size(estimate.quota)}${
                    estimate.usage > totalBytes ? `, including about ${size(estimate.usage - totalBytes)} of browser overhead` : ""
                  }.`
                : "unavailable (needs a secure address and Safari 17 or later)."}
            </li>
            {usage?.backend === "local" ? (
              <li>Chats: about {size(chatsBytes)} in localStorage.</li>
            ) : (
              <li>
                Chats: about {size(chatsBytes)} in the <code>{TRANSCRIPT_DB}</code> IndexedDB database.
              </li>
            )}
            <li>Layout, preferences, dismissals and sign-in: about {size(usage?.prefsBytes ?? 0)} in localStorage.</li>
            <li>
              Persistent storage: {persistDetail(canPersist, persist)}. Saving: {saving ? "ok" : "failing"}.
            </li>
          </ul>
        </details>
      </div>

      <ConfirmDialog
        open={confirm !== null}
        destructive
        title={confirm ? confirmTitle(confirm, nameOf) : undefined}
        confirmLabel={confirm?.kind === "quick" ? "Clear quick chats" : confirm?.kind === "free" ? "Free up space" : "Clear chats"}
        onClose={() => setConfirm(null)}
        onConfirm={() => confirm && void run(confirm)}
      >
        {confirm?.kind === "agent" ? (
          <div className="storage-dialog">
            <p>
              Removes {plural(confirm.row.clearable.chats, "saved chat", "saved chats")} ({size(confirm.row.clearable.bytes)}) from this
              browser.
            </p>
            <p>
              Stays: the server's copy of the last 24 hours of each chat, {nameOf(confirm.row.slug)}'s memory and settings, and anything
              on your other devices. Chats open in a tab stay too.
            </p>
          </div>
        ) : confirm?.kind === "gone" ? (
          <div className="storage-dialog">
            <p>
              Removes {plural(goneChats, "saved chat", "saved chats")} ({size(goneBytes)}) from {joinAnd(gone.map((r) => nameOf(r.slug)))}.
            </p>
            <p>
              Stays: the server's copy of the last 24 hours of each chat, every agent still in your fleet, and anything on your other
              devices. Chats open in a tab stay too.
            </p>
          </div>
        ) : confirm?.kind === "quick" ? (
          <div className="storage-dialog">
            <p>
              Removes {plural(usage?.quick.threads ?? 0, "quick chat thread", "quick chat threads")} ({size(usage?.quick.bytes ?? 0)})
              from the command palette and direct messages.
            </p>
            <p>
              Stays: the server's copy of the last 24 hours of each chat, every agent's regular chats, and anything on your other
              devices. Chats open in a tab stay too.
            </p>
          </div>
        ) : confirm?.kind === "free" ? (
          <div className="storage-dialog">
            <p>Clears old chats from this browser until new messages fit, in this order:</p>
            <ol>
              <li>Agents no longer in your fleet</li>
              <li>Quick chats from other windows</li>
              <li>Agents you haven't opened in a day</li>
              <li>{hereName}'s oldest chats</li>
            </ol>
            <p>
              Never cleared: chats open in a tab, and {hereName}'s reply in progress. The server keeps the last 24 hours of each chat.
            </p>
          </div>
        ) : null}
      </ConfirmDialog>
    </SettingsSubPanel>
  );
}

function displayName(agent: FleetAgent): string {
  return agent.label || agent.name;
}

function confirmTitle(confirm: Confirm, nameOf: (slug: string) => string): string {
  switch (confirm.kind) {
    case "agent":
      return `Clear ${nameOf(confirm.row.slug)}'s chats from this browser?`;
    case "gone":
      return "Clear agents no longer in your fleet?";
    case "quick":
      return "Clear quick chats from this browser?";
    case "free":
      return "Free up space?";
  }
}

function keepDescription(secure: boolean, persist: PersistState): string {
  if (!secure) return "Only works on a secure (https) address or in the desktop app. This console is on plain http.";
  if (persist === "granted") {
    return "The browser won't clear this console's data on its own. The console still clears old chats itself when it runs out of room.";
  }
  if (persist === "refused") {
    return "This browser chose not to keep it this time. That's the browser's call and nothing is wrong: your chats save as usual. Browsers often say yes later, for example once you've used the console for a while or bookmarked it.";
  }
  return "Asks the browser not to clear this console's data on its own, for example when your disk is low.";
}

function persistDetail(available: boolean, persist: PersistState): string {
  if (persist === "granted") return "granted";
  if (!available) return "not available on this address";
  if (persist === "refused") return "requested and not granted";
  return "not requested";
}
