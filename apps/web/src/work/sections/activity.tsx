/**
 * Activity — the Work view's §2.10 section (#4955 comment 6739622302).
 *
 * The activity feed (ADR 0022) is the agent's own provenance log: one row per agent-initiated
 * turn, with WHAT triggered it. The raw `origin` is one of `scheduler | watch | inbox |
 * webhook | a2a | operator` (src/activity, src/a2a_impl/executor.py), empty ⇒ operator; the
 * raw `state` is the terminal turn outcome `completed | failed | canceled`; `priority` carries
 * the inbox tier `now | next | later` (src/inbox/store.py) when the origin is the inbox.
 *
 * The section shows the last 24 h, with Show more to reveal older rows (Q2). Opening a row
 * renders a `WorkDetail` that names the trigger in plain words and, for an inbox `now` item,
 * flags it Urgent.
 *
 * Registration is a module side effect: importing this file installs the "activity" renderer
 * in the `WorkView` registry exactly once.
 */
import { useState } from "react";
import { Badge, Button, Callout, Empty } from "@protolabsai/ui/primitives";
import { Alert, Skeleton } from "@protolabsai/ui/data";
import { registerWorkSection, type WorkSectionContext } from "../WorkView";
import { WorkRow } from "../WorkRow";
import { WorkDetail, type WorkField } from "../WorkDetail";
import { relTime } from "../tone";
import type { WorkItem } from "../useWorkItems";
import type { ActivityEntry } from "../../lib/types";

/** The window the section shows before Show more (Q2). */
const DAY_MS = 86_400_000;

/** "Triggered by" in plain words (§2.10) — the raw `origin` mapped to a human label. A
 *  scheduler OR watch fire is "Scheduled"; the operator (and a bare live reply) is "You". */
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

/** The inbox `now` tier is the only urgent one (§2.10). `priority` is populated only for
 *  inbox-triggered turns, so the tier alone identifies it. */
export function isUrgent(entry: Pick<ActivityEntry, "priority">): boolean {
  return entry.priority === "now";
}

/** Epoch ms for a feed row's `created_at`. The store writes a naive-ish ISO string; treat a
 *  `T`-form with no zone as UTC (matching WorkOverview's terminal-time parsing). */
function createdMs(entry: ActivityEntry): number {
  const value = entry.created_at;
  if (!value) return NaN;
  return Date.parse(/T/.test(value) && !/(Z|[+-]\d\d:\d\d)$/.test(value) ? `${value}Z` : value);
}

function ActivityDetail({ item }: { item: WorkItem }) {
  const entry = item.raw as ActivityEntry;
  const fields: WorkField[] = [
    { label: "Triggered by", value: triggeredByLabel(entry.origin) },
    // Urgent shows ONLY for the inbox `now` tier — never for any other trigger.
    { label: "Urgent", value: isUrgent(entry) ? <Badge status="warning">Urgent</Badge> : null },
    { label: "When", value: entry.created_at ? relTime(entry.created_at).text : null },
    { label: "In response to", value: entry.stimulus?.trim() || null },
    { label: "Response", value: entry.text?.trim() || null },
  ];
  return (
    <WorkDetail
      title={item.title || "Activity"}
      status={item.status}
      fields={fields}
      actions={[]}
      technical={{
        Origin: entry.origin || "operator",
        Trigger: entry.trigger || "",
        Priority: entry.priority || "",
        State: entry.state,
        "Task id": entry.task_id || "",
      }}
    />
  );
}

function ActivityRow({ item, onOpen }: { item: WorkItem; onOpen: () => void }) {
  const entry = item.raw as ActivityEntry;
  return (
    <WorkRow
      status={item.status}
      title={item.title}
      meta={triggeredByLabel(entry.origin)}
      focusKey={`${item.kind}:${item.id}`}
      onOpen={onOpen}
    />
  );
}

function ActivityList({ ctx }: { ctx: WorkSectionContext }) {
  const source = ctx.sources.activity;
  const [showMore, setShowMore] = useState(false);
  const cutoff = Date.now() - DAY_MS;
  // Older-than-24h rows hide behind Show more; a row with no parseable time is kept visible.
  const older = source.items.filter((item) => createdMs(item.raw as ActivityEntry) < cutoff);
  const visible = showMore ? source.items : source.items.filter((item) => !older.includes(item));

  return (
    <div className="work-activity">
      {source.status === "error" ? (
        <Alert status="warning">
          Couldn&apos;t load activity.{" "}
          <Button size="sm" onClick={source.retry}>
            Retry
          </Button>
        </Alert>
      ) : source.status === "stale" ? (
        <Callout tone="warning">
          Activity may be out of date.{" "}
          <Button size="sm" onClick={source.retry}>
            Retry
          </Button>
        </Callout>
      ) : null}
      {source.status === "loading" ? (
        <div aria-label="Loading activity" aria-busy="true">
          <Skeleton lines={3} />
        </div>
      ) : source.items.length === 0 && source.status !== "error" ? (
        <Empty title="No activity yet" />
      ) : (
        <>
          {visible.map((item) => (
            <ActivityRow key={`${item.kind}:${item.id}`} item={item} onOpen={() => ctx.onOpen(item)} />
          ))}
          {!showMore && older.length > 0 ? (
            <Button variant="ghost" size="sm" onClick={() => setShowMore(true)}>
              Show more
            </Button>
          ) : null}
        </>
      )}
    </div>
  );
}

registerWorkSection("activity", {
  label: "Activity",
  render(ctx) {
    const itemId = ctx.state.itemId;
    if (!itemId) return <ActivityList ctx={ctx} />;
    const item = ctx.sources.activity.items.find(
      (i) => `${i.kind}:${i.id}` === itemId || i.id === itemId,
    );
    if (!item) return null;
    return <ActivityDetail item={item} />;
  },
});
