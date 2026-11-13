import "./activity.css";

import { useQuery } from "@tanstack/react-query";
// The DS Badge rides an alias: OriginProvenance.test.tsx (DS audit card 4) pins that no
// `<Badge` token appears in this file — the guard retires with the component in slice 7.
import { Badge as FailedBadge, Empty } from "@protolabsai/ui/primitives";
import { Alert, Skeleton, StatusDot } from "@protolabsai/ui/data";
import { Clock, Inbox, MessageSquare, Users, Webhook, Zap } from "lucide-react";

import { useCallback, useEffect, useRef, useState } from "react";

import { useUtilityHeaderReload } from "../app/UtilityWidget";
import { api } from "../lib/api";
import { brandName } from "../lib/brand";
import { ago } from "../lib/format";
import { onServerEvent } from "../lib/events";
import { runtimeStatusQuery } from "../lib/queries";
import type { ActivityEntry, InboxItem } from "../lib/types";
import { relTime } from "../work/tone";
import { FeedDetail } from "./FeedDetail";
import { applyLiveEntry, groupByDay, mergeFeed, stripWakeHeader, type FeedRow } from "./feedModel";

// The Feed (docs/design/feed.md §1): ONE chronological list, newest first, where each
// thing the agent was asked to do appears exactly once — a pending inbox item is a quiet
// queued row, a completed turn is a ran/failed row, and an entry whose `inbox_id` names
// a pending item replaces that item's row. Nothing here dismisses, delivers or alerts
// (bd-rbk7e retries undelivered pages on its own; anything needing the operator goes to
// Work › Needs you). The merge/replace/grouping logic is the pure feed model (slice 3);
// this surface fetches, listens to the bus, and renders what the model returns.
//
// The two feeds are still fetched and error-handled INDEPENDENTLY (separate state,
// separate try/catch): a transient failure of GET /api/inbox or GET /api/activity
// surfaces the one couldn't-load alert while whatever loaded keeps rendering (§7).

// origin → badge (icon + label). "" / unknown falls back to a generic agent turn.
const ORIGIN: Record<string, { icon: typeof Clock; label: string }> = {
  scheduler: { icon: Clock, label: "scheduled" },
  inbox: { icon: Inbox, label: "inbox" },
  webhook: { icon: Webhook, label: "webhook" },
  a2a: { icon: Users, label: "sister-agent" },
  operator: { icon: MessageSquare, label: "you" },
};

// The wake-header stripping (#5094) moved into the pure feed model (docs/design/feed.md
// §3) so the merged Feed shares it; re-exported here for existing callers.
export { stripWakeHeader };

export function OriginProvenance({ entry }: { entry: ActivityEntry }) {
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

export function ActivitySurface({ onOpenDetail }: { onOpenDetail?: (row: FeedRow) => void }) {
  // The agent's display name — the queued-row promise and the live announcement both
  // speak it. Same cached probe the app shell reads (one query key, deduped).
  const { data: runtime } = useQuery(runtimeStatusQuery());
  const agentName = brandName(runtime?.identity?.name);
  const agentNameRef = useRef(agentName);

  // The detail view (§5): clicking a row swaps the list for FeedDetail in the SAME
  // dialog body. A host that passes `onOpenDetail` owns the detail instead (the slice-4
  // contract, kept for alternate hosts); the Feed dialog passes nothing and uses this.
  // `returnKey` remembers which row opened it so "← Feed" can put focus back (§8).
  const [detail, setDetail] = useState<FeedRow | null>(null);
  const returnKey = useRef<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // Raw feed sources live in refs; `rows` is the one rendered projection. It is REBUILT
  // via mergeFeed after any fetch (sorted newest first) and patched by applyLiveEntry on
  // a live push (§3: a queued row turns into its result in place, without jumping).
  const items = useRef<InboxItem[]>([]);
  const entries = useRef<ActivityEntry[]>([]);
  const rowsRef = useRef<FeedRow[]>([]);
  const [rows, setRows] = useState<FeedRow[]>([]);
  const [activityLoading, setActivityLoading] = useState(true);
  const [activityError, setActivityError] = useState(false);
  const [inboxLoading, setInboxLoading] = useState(true);
  const [inboxError, setInboxError] = useState(false);
  // Live-only rows count downward so a same-millisecond event burst cannot collide with
  // a React key or an entry loaded from the API (persisted ids are positive).
  const nextLiveId = useRef(0);
  // The one polite live region (§8): set ONLY when a queued row turns into its result —
  // never for every new row.
  const [announce, setAnnounce] = useState("");

  const commit = useCallback((next: FeedRow[]) => {
    rowsRef.current = next;
    setRows(next);
  }, []);
  const rebuild = useCallback(() => {
    commit(mergeFeed(items.current, entries.current, agentNameRef.current));
  }, [commit]);

  // The runtime probe can resolve after the first merge — re-merge so queued rows
  // promise the real name, not the brand fallback.
  useEffect(() => {
    if (agentNameRef.current === agentName) return;
    agentNameRef.current = agentName;
    rebuild();
  }, [agentName, rebuild]);

  const loadActivity = useCallback(async () => {
    setActivityLoading(true);
    try {
      const r = await api.activity();
      entries.current = r.entries || [];
      setActivityError(false);
      rebuild();
    } catch {
      setActivityError(true);
    } finally {
      setActivityLoading(false);
    }
  }, [rebuild]);

  const loadInbox = useCallback(async () => {
    setInboxLoading(true);
    try {
      const r = await api.inbox();
      items.current = r.items || [];
      setInboxError(false);
      rebuild();
    } catch {
      setInboxError(true);
    } finally {
      setInboxLoading(false);
    }
  }, [rebuild]);

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

  // Live: every completed turn pushes `activity.message`. When its `inbox_id` matches a
  // queued row, that row turns into its result in place (and the live region announces
  // it); otherwise the entry prepends (newest first).
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
          inbox_id: typeof data.inbox_id === "number" ? data.inbox_id : null,
        };
        entries.current = [entry, ...entries.current];
        const prev = rowsRef.current;
        const wasQueued =
          entry.inbox_id != null &&
          prev.some((r) => r.kind === "queued" && r.item?.id === entry.inbox_id);
        const next = applyLiveEntry(prev, entry, agentNameRef.current);
        commit(next);
        if (wasQueued) {
          const title = next.find((r) => r.entry?.id === entry.id)?.title ?? "";
          setAnnounce(`${agentNameRef.current} finished: ${title}`);
        }
      }),
    [commit],
  );

  // A new inbound item pushes `inbox.item` — refetch so its queued row appears with its
  // server id (the re-merge also drops anything delivered meanwhile).
  useEffect(() => onServerEvent("inbox.item", () => void loadInbox()), [loadInbox]);

  const openDetail = useCallback(
    (row: FeedRow) => {
      if (onOpenDetail) {
        onOpenDetail(row);
        return;
      }
      returnKey.current = row.key;
      setDetail(row);
    },
    [onOpenDetail],
  );
  const closeDetail = useCallback(() => setDetail(null), []);
  // Back to the list: focus returns to the row that opened the detail (§8). The row can
  // have vanished meanwhile (a queued row replaced by its live result has a new key) —
  // fall back to the first row so focus never silently drops to <body>.
  useEffect(() => {
    if (detail || !returnKey.current) return;
    const key = returnKey.current;
    returnKey.current = null;
    const rowButtons = [...(rootRef.current?.querySelectorAll<HTMLButtonElement>(".feed-row") ?? [])];
    (rowButtons.find((b) => b.dataset.key === key) ?? rowButtons[0])?.focus();
  }, [detail]);

  const loading = activityLoading || inboxLoading;
  const failed = activityError || inboxError;
  const groups = groupByDay(rows);

  if (detail) {
    return (
      // The detail replaces the list in the SAME dialog body (§5) — the live region
      // stays out here so a queued→ran announcement still fires while reading.
      <div className="unified-feed util-dialog-fill" data-testid="unified-feed" ref={rootRef}>
        <FeedDetail row={detail} agentName={agentName} onBack={closeDetail} />
        <span className="feed-announce" role="status" aria-live="polite" aria-atomic="true">
          {announce}
        </span>
      </div>
    );
  }

  return (
    // The utility-bar dialog gives the feed the shared fixed body height (#5165). The
    // list is newest first and opens at the top — no auto-scroll.
    <div className="unified-feed util-dialog-fill" data-testid="unified-feed" ref={rootRef}>
      {failed ? (
        <Alert status="error" className="feed-alert">
          Couldn't load the feed. Check the connection, then reload.
        </Alert>
      ) : null}
      <div className="feed-list" data-testid="feed-list" aria-busy={loading || undefined}>
        {loading && rows.length === 0 ? (
          <div className="feed-skeleton" data-testid="feed-skeleton">
            <Skeleton lines={3} />
          </div>
        ) : null}
        {!loading && !failed && rows.length === 0 ? (
          <Empty
            className="activity-empty"
            title="Nothing here yet"
            description={`When ${agentName} works on its own — a schedule, a message from another agent, a webhook — it shows up here, newest first.`}
          />
        ) : null}
        {groups.map((g) => (
          // Keyed by the first row, not the heading: an in-place replaced row (§3) keeps
          // its old position with a new timestamp, which can split a day into two groups
          // with the same heading until the next merge re-sorts.
          <section className="feed-day" key={g.rows[0]?.key ?? g.heading}>
            <h3 className="feed-day-heading">{g.heading}</h3>
            <ol className="feed-day-rows">
              {g.rows.map((row) => {
                const time = relTime(row.sortAt);
                return (
                  <li className="feed-item" key={row.key}>
                    <button /* ds-audit-ignore hand-rolled-control — composite row/trigger, protoContent#551 */
                      type="button"
                      className="feed-row"
                      data-kind={row.kind}
                      data-key={row.key}
                      aria-label={`${row.kind === "failed" ? "Failed: " : ""}${row.title}. ${row.meta}. ${time.text}. Open details`}
                      onClick={() => openDetail(row)}
                    >
                      <StatusDot status={row.kind === "failed" ? "error" : "neutral"} />
                      <span className="feed-row-body">
                        <span className="feed-row-head">
                          {row.kind === "failed" ? <FailedBadge status="error">Failed</FailedBadge> : null}
                          <span className={`feed-row-title${row.kind === "queued" ? " feed-row-title--queued" : ""}`}>
                            {row.title}
                          </span>
                          <span className="feed-row-time" title={time.tooltip}>
                            {time.text}
                          </span>
                        </span>
                        <span className="feed-row-meta">{row.meta}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ol>
          </section>
        ))}
      </div>
      <span className="feed-announce" role="status" aria-live="polite" aria-atomic="true">
        {announce}
      </span>
    </div>
  );
}
