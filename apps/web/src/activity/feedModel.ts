import { parseInstant } from "../lib/format";
import type { ActivityEntry, InboxItem } from "../lib/types";

// The Feed data model (docs/design/feed.md §1, §3, §4): one chronological list, newest
// first, where each thing the agent was asked to do appears exactly once. A pending
// inbox item is a quiet "queued" row; a completed agent turn is a "ran" (or "failed")
// row; an entry whose `inbox_id` names a pending item REPLACES that item's row (§3),
// so a stimulus and its result never show as two rows. Pure data logic — no React, no
// fetch; the Feed surface (slice 4) renders what these functions return.

export type FeedRow = {
  key: string;
  kind: "queued" | "ran" | "failed";
  /** Ordering key (§3): the inbox item's `created_at` for queued, the entry's for
   *  ran/failed. ISO string as the stores hand it out. */
  sortAt: string;
  title: string;
  meta: string;
  item?: InboxItem;
  entry?: ActivityEntry;
};

export type FeedDayGroup = { heading: string; rows: FeedRow[] };

/** The scheduler's one-line wake header (`src/scheduler/local.py`, ADR 0079), which it
 *  prepends to a scheduled/watch/wait/goal prompt. It is runtime framing for the model,
 *  not the job's words. */
const WAKE_HEADER = /^\s*\[Autonomous wake\b[^\]]*\]\s*/;

/** The operator-facing stimulus: the raw stimulus minus the internal wake header (#5094).
 *  Every rendering of a stimulus (row title, meta, reader) goes through here. Empty when
 *  the stimulus was nothing but the header. Lives here (not ActivitySurface) so the pure
 *  feed model owns it; ActivitySurface re-exports it for its existing callers. */
export function stripWakeHeader(stimulus: string | undefined): string {
  return stimulus?.replace(WAKE_HEADER, "").trim() ?? "";
}

/** "Triggered by" in plain words — the raw `origin` mapped to a human label (§4). A
 *  scheduler OR watch fire is "Scheduled"; the operator (and a bare live reply) is
 *  "You". (The old Work Activity section's `triggeredByLabel`, re-homed here — the
 *  section was removed with Work consolidation S4c.) */
export function triggeredByLabel(origin: string): string {
  switch (origin) {
    case "scheduler":
    case "watch":
      return "Scheduled";
    case "inbox":
      return "Inbox";
    case "webhook":
      return "Webhook";
    case "a2a":
      return "Another agent";
    case "operator":
    case "":
      return "You";
    default:
      return origin.charAt(0).toUpperCase() + origin.slice(1);
  }
}

/** The row's source label (§4): origin label plus the trigger when there is one —
 *  "Scheduled · ds-drift-watch", "Another agent · protoEngineer", "Inbox · github",
 *  bare "You" for an operator turn. */
export function sourceLabel(origin: string, trigger: string | null | undefined): string {
  const label = triggeredByLabel(origin);
  return trigger ? `${label} · ${trigger}` : label;
}

/** Epoch ms for a feed timestamp via the shared `parseInstant` (offsetless = UTC).
 *  0 when missing/unparseable, so broken rows sink to the bottom instead of NaN
 *  poisoning the sort. */
function timeMs(value: string | undefined): number {
  return parseInstant(value) ?? 0;
}

/** Ran/failed title: the first non-empty line of the agent's response (§4). */
function firstLine(text: string | undefined): string {
  for (const line of (text ?? "").split("\n")) {
    const t = line.trim();
    if (t) return t;
  }
  return "";
}

function queuedRow(item: InboxItem, agentName: string): FeedRow {
  // Once the automatic retry budget is spent the sweeps will never pick this page up
  // again (types.ts `retries_exhausted`), so promising "runs when the agent is free"
  // would be a lie — same wording ActivitySurface's queued-now flag uses.
  const status = item.retries_exhausted
    ? "Queued — automatic retries used up"
    : `Queued — runs when ${agentName} is free`;
  return {
    key: `item:${item.id}`,
    kind: "queued",
    sortAt: item.created_at,
    title: stripWakeHeader(item.text),
    meta: `${sourceLabel("inbox", item.source)} · ${status}`,
    item,
  };
}

function entryRow(entry: ActivityEntry): FeedRow {
  const stimulus = stripWakeHeader(entry.stimulus);
  const label = sourceLabel(entry.origin, entry.trigger);
  return {
    key: `entry:${entry.id}`,
    kind: entry.state === "failed" ? "failed" : "ran",
    sortAt: entry.created_at,
    title: firstLine(entry.text),
    meta: stimulus ? `${label} · in response to “${stimulus}”` : label,
    entry,
  };
}

/** The merged Feed (§1, §3): pending inbox items (never delivered ones) as queued rows,
 *  every activity entry as a ran/failed row, EXCEPT that a pending item claimed by some
 *  entry's `inbox_id` is not shown — the entry is. Newest first. */
export function mergeFeed(items: InboxItem[], entries: ActivityEntry[], agentName: string): FeedRow[] {
  const claimed = new Set<number>();
  for (const e of entries) {
    if (e.inbox_id != null) claimed.add(e.inbox_id);
  }
  const rows: FeedRow[] = [];
  for (const item of items) {
    if (item.delivered_at || claimed.has(item.id)) continue;
    rows.push(queuedRow(item, agentName));
  }
  for (const e of entries) rows.push(entryRow(e));
  rows.sort((a, b) => timeMs(b.sortAt) - timeMs(a.sortAt));
  return rows;
}

/** A live `activity.message` (§3): when the entry's `inbox_id` matches a queued row,
 *  the result replaces that row IN PLACE — same index, so the row visibly turns into
 *  its result without jumping; it re-sorts by its own `created_at` on the next
 *  `mergeFeed`. Otherwise the entry prepends (newest first). An entry already present
 *  is ignored, so replaying the event is a no-op. */
export function applyLiveEntry(rows: FeedRow[], entry: ActivityEntry, agentName: string): FeedRow[] {
  void agentName; // reserved: a future live row shape may need it (queued rows do)
  if (rows.some((r) => r.entry?.id === entry.id)) return rows;
  const row = entryRow(entry);
  const at =
    entry.inbox_id == null
      ? -1
      : rows.findIndex((r) => r.kind === "queued" && r.item?.id === entry.inbox_id);
  if (at < 0) return [row, ...rows];
  const next = rows.slice();
  next[at] = row;
  return next;
}

/** Day headings over an already-sorted list (§4): "Today", "Yesterday", then "Nov 10",
 *  in the viewer's local time. Consecutive same-day rows share one group. */
export function groupByDay(rows: FeedRow[], now: Date = new Date()): FeedDayGroup[] {
  const dayKey = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  const today = dayKey(now);
  const yesterday = dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  const groups: FeedDayGroup[] = [];
  let openKey = "";
  for (const row of rows) {
    const day = new Date(timeMs(row.sortAt));
    const key = dayKey(day);
    if (!groups.length || key !== openKey) {
      openKey = key;
      groups.push({
        heading:
          key === today
            ? "Today"
            : key === yesterday
              ? "Yesterday"
              : day.toLocaleDateString("en-US", { month: "short", day: "numeric" }),
        rows: [],
      });
    }
    groups[groups.length - 1].rows.push(row);
  }
  return groups;
}
