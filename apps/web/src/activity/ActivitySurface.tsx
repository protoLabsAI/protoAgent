import "./activity.css";

import { Button, Empty } from "@protolabsai/ui/primitives";
import {
  AlertTriangle,
  Check,
  Clock,
  CornerDownRight,
  Inbox,
  Maximize2,
  MessageSquare,
  Users,
  Webhook,
  Zap,
} from "lucide-react";

import { useCallback, useEffect, useRef, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { openDocument } from "../docviewer";
import { useUtilityHeaderReload } from "../app/UtilityWidget";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { onServerEvent } from "../lib/events";
import { isPendingNowInboxPage } from "../lib/queries";
import type { ActivityEntry, InboxItem } from "../lib/types";

// The unified feed (#3029) merges the two former utility-bar widgets — the inbound
// Inbox (ADR 0003) and the read-only Activity provenance timeline (ADR 0022) — into
// one dialog body behind a single pill. PENDING inbound stimuli render at the top
// (priority + dismiss); COMPLETED agent-initiated turns render below (provenance
// badges, markdown, open-in-reader), appending live via the `activity.message` push.
//
// The two feeds are fetched and error-handled INDEPENDENTLY (separate state, separate
// try/catch): a transient failure of GET /api/inbox or GET /api/activity surfaces its
// own error strip and never blanks the other section.

// origin → badge (icon + label). "" / unknown falls back to a generic agent turn.
const ORIGIN: Record<string, { icon: typeof Clock; label: string }> = {
  scheduler: { icon: Clock, label: "scheduled" },
  inbox: { icon: Inbox, label: "inbox" },
  webhook: { icon: Webhook, label: "webhook" },
  a2a: { icon: Users, label: "sister-agent" },
  operator: { icon: MessageSquare, label: "you" },
};

// inbox priority tier → tone class (shared with the completed-entry priority badge).
const PRIORITY_TONE: Record<string, string> = { now: "now", next: "next", later: "later" };

function Badge({ entry }: { entry: ActivityEntry }) {
  const o = ORIGIN[entry.origin] ?? { icon: Zap, label: entry.origin || "agent" };
  const Icon = o.icon;
  return (
    <div className="activity-prov">
      <span className={`activity-origin activity-origin-${entry.origin || "agent"}`}>
        <Icon size={12} /> {o.label}
      </span>
      {entry.trigger ? <span className="activity-trigger">{entry.trigger}</span> : null}
      {entry.priority ? <span className={`inbox-pri inbox-pri-${entry.priority}`}>{entry.priority}</span> : null}
      {entry.created_at ? <span className="activity-time">{ago(entry.created_at)}</span> : null}
    </div>
  );
}

export function ActivitySurface() {
  // --- Completed activity timeline. Held newest-first (as the API returns), rendered
  //     oldest-first. Persisted rows use positive database IDs; live-only rows count
  //     downward so a same-millisecond event burst cannot collide with a React key or
  //     an entry loaded from the API. ---
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const [activityLoading, setActivityLoading] = useState(true);
  const [activityError, setActivityError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const nextLiveId = useRef(0);

  // --- Pending inbox. `dismissed` survives the live-event refetch cycle so a delivered
  //     item stays hidden even if the server briefly re-includes it before dropping it. ---
  const [items, setItems] = useState<InboxItem[]>([]);
  const [inboxLoading, setInboxLoading] = useState(true);
  const [inboxError, setInboxError] = useState<string | null>(null);
  const dismissed = useRef<Set<number>>(new Set());

  const loadActivity = useCallback(async () => {
    setActivityLoading(true);
    try {
      const r = await api.activity();
      setEntries(r.entries || []);
      setActivityError(null);
    } catch (e) {
      setActivityError(errMsg(e));
    } finally {
      setActivityLoading(false);
    }
  }, []);

  const loadInbox = useCallback(async () => {
    setInboxLoading(true);
    try {
      const r = await api.inbox();
      setItems((r.items || []).filter((i) => !dismissed.current.has(i.id)));
      setInboxError(null);
    } catch (e) {
      setInboxError(errMsg(e));
    } finally {
      setInboxLoading(false);
    }
  }, []);

  // Independent initial loads — one rejecting never blocks or blanks the other.
  useEffect(() => {
    void loadActivity();
  }, [loadActivity]);
  useEffect(() => {
    void loadInbox();
  }, [loadInbox]);

  // The reload lives in the dialog header (UtilityWidget) — it refetches BOTH feeds,
  // still independently. No second panel header here.
  const reload = useCallback(() => {
    void loadActivity();
    void loadInbox();
  }, [loadActivity, loadInbox]);
  useUtilityHeaderReload(reload, activityLoading || inboxLoading);

  // Live append: every completed Activity turn pushes `activity.message` with the
  // assistant text + provenance. Prepend (newest-first store order).
  useEffect(
    () =>
      onServerEvent("activity.message", (data) => {
        const text = typeof data.text === "string" ? data.text : "";
        if (!text) return;
        const entry: ActivityEntry = {
          id: --nextLiveId.current,
          created_at: new Date().toISOString(),
          origin: typeof data.origin === "string" ? data.origin : "",
          trigger: typeof data.trigger === "string" ? data.trigger : "",
          priority: typeof data.priority === "string" ? data.priority : "",
          state: typeof data.state === "string" ? data.state : "completed",
          text,
          task_id: typeof data.task_id === "string" ? data.task_id : "",
          stimulus: typeof data.stimulus === "string" ? data.stimulus : "",
        };
        setEntries((prev) => [entry, ...prev]);
      }),
    [],
  );

  // Live: a new inbound item pushes `inbox.item` — refetch to pick it up with its
  // server id (already-dismissed ids stay filtered out).
  useEffect(() => onServerEvent("inbox.item", () => void loadInbox()), [loadInbox]);

  // Dismiss = mark delivered (POST /api/inbox/{id}/deliver). Optimistic: hide on click
  // and remember the id so the live refetch can't bring it back (it won't return once
  // delivered anyway; the server-side deliver is idempotent).
  const dismiss = useCallback((id: number) => {
    dismissed.current.add(id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    void api.deliverInbox(id).catch(() => {});
  }, []);

  // Keep the newest completed entry (bottom, since we render chronological) in view.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [entries]);

  const chronological = [...entries].reverse();
  // A pending priority-`now` item is only in this pull queue because its automatic delivery
  // failed (#3351) — hoist those blocked pages above the ordinary queued stimuli and render
  // them conspicuously, while leaving the next/later list exactly as it was.
  const nowPages = items.filter(isPendingNowInboxPage);
  const queued = items.filter((i) => !isPendingNowInboxPage(i));
  const nothing =
    items.length === 0 && chronological.length === 0 && !activityLoading && !inboxLoading;

  return (
    <div className="unified-feed util-dialog-fill" data-testid="unified-feed">
      {/* PENDING (top): inbound stimuli awaiting processing (ADR 0003). */}
      {inboxError ? (
        <div className="activity-error" role="alert">
          {inboxError}
        </div>
      ) : null}
      {items.length > 0 ? (
        <div className="inbox-list" data-testid="feed-pending">
          {/* Blocked priority-`now` pages first: a now item is only pending because its
              automatic delivery failed (#3351), so it's a diagnostic fallback the operator must
              triage — flagged conspicuously with full source/priority/text context. Dismiss stays
              an EXPLICIT operator action (mark delivered); nothing here silently delivers it. */}
          {nowPages.map((item) => (
            <div className="inbox-item inbox-item--stuck" data-testid="inbox-now-page" key={item.id}>
              <div className="inbox-item-head">
                <span className="inbox-pri inbox-pri-now">{item.priority}</span>
                <span
                  className="inbox-stuck-flag"
                  title="This priority-now page could not be delivered automatically — it is waiting for the pull fallback. Deliver it into a turn or dismiss it."
                >
                  <AlertTriangle size={12} aria-hidden /> undelivered page
                </span>
                {item.source ? <span className="inbox-source">{item.source}</span> : null}
                <Button
                  icon
                  variant="ghost"
                  className="inbox-dismiss"
                  type="button"
                  onClick={() => dismiss(item.id)}
                  title="Deliver this page (mark delivered) and dismiss"
                >
                  <Check size={15} />
                </Button>
              </div>
              <div className="inbox-text">{item.text}</div>
            </div>
          ))}
          {queued.map((item) => (
            <div className="inbox-item" key={item.id}>
              <div className="inbox-item-head">
                <span className={`inbox-pri inbox-pri-${PRIORITY_TONE[item.priority] || "next"}`}>
                  {item.priority}
                </span>
                {item.source ? <span className="inbox-source">{item.source}</span> : null}
                <Button
                  icon
                  variant="ghost"
                  className="inbox-dismiss"
                  type="button"
                  onClick={() => dismiss(item.id)}
                  title="Mark delivered (dismiss)"
                >
                  <Check size={15} />
                </Button>
              </div>
              <div className="inbox-text">{item.text}</div>
            </div>
          ))}
        </div>
      ) : null}

      {/* COMPLETED (below): the read-only provenance timeline (ADR 0022). */}
      {activityError ? (
        <div className="activity-error" role="alert">
          {activityError}
        </div>
      ) : null}
      <div className="activity-feed" ref={scrollRef} data-testid="feed-completed">
        {nothing ? (
          <Empty
            className="activity-empty"
            title="Nothing yet"
            description="Pending inbound items and completed agent turns land here — each tagged with what triggered it."
          />
        ) : null}
        {chronological.map((e) => (
          <div
            className="activity-entry"
            key={e.id}
            data-entry-id={e.id}
            data-origin={e.origin}
            data-state={e.state}
          >
            <div className="activity-entry-head">
              <Badge entry={e} />
              {/* Open the full entry in the shared full-screen reader (ADR 0062) —
                  the same view the chat report card opens. */}
              <button
                type="button"
                className="pl-iconbtn activity-entry-open"
                aria-label="Open in reader"
                title="Open in reader"
                onClick={() =>
                  openDocument({
                    title: ORIGIN[e.origin]?.label ?? e.origin ?? "Activity",
                    subtitle:
                      [e.trigger, e.task_id ? `task ${e.task_id}` : "", e.created_at ? ago(e.created_at) : ""]
                        .filter(Boolean)
                        .join(" · ") || undefined,
                    content: e.stimulus
                      ? `> **In response to**\n>\n> ${e.stimulus.replace(/\n/g, "\n> ")}\n\n---\n\n${e.text}`
                      : e.text,
                  })
                }
              >
                <Maximize2 size={13} />
              </button>
            </div>
            {/* Explicit stimulus attribution (#1375): the input this response is replying to,
                so the feed isn't an unanchored wall of agent output. Full text on hover. */}
            {e.stimulus ? (
              <div className="activity-stimulus" title={e.stimulus}>
                <CornerDownRight size={12} aria-hidden />
                <span className="activity-stimulus-label">in response to</span>
                <span className="activity-stimulus-text">{e.stimulus}</span>
              </div>
            ) : null}
            <div className="activity-content">
              <Markdown>{e.text}</Markdown>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
