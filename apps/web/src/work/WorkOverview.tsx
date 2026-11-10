import { useState } from "react";
import { Alert, Skeleton } from "@protolabsai/ui/data";
import { Button, Callout, Empty } from "@protolabsai/ui/primitives";
import { laneFor, type WorkLane } from "./tone";
import { WorkRow } from "./WorkRow";
import type { WorkItem, WorkItems } from "./useWorkItems";

export const workSections = {
  overview: "Overview", decisions: "Decisions", goals: "Goals", watches: "Watches",
  tasks: "Tasks", schedules: "Schedules", background: "Background agents",
  activity: "Activity", directives: "Directives",
};

const lanes: { id: WorkLane; label: string; help: string }[] = [
  { id: "needs-you", label: "Needs you", help: "Waiting for your decision." },
  { id: "doing", label: "Doing", help: "Work in progress." },
  { id: "waiting", label: "Waiting on", help: "Waiting for a condition or dependency." },
  { id: "next", label: "Next", help: "Work queued for later." },
  { id: "done", label: "Done", help: "Finished in the last 24 hours." },
];

// Normalized items retain their wire rows; terminal timestamps differ by source.
function finishedAt(item: WorkItem): number {
  const raw = item.raw as Record<string, unknown> | null;
  const value = raw?.finished_at ?? raw?.completed_at ?? raw?.closed_at ?? raw?.answered_at
    ?? raw?.last_fire ?? raw?.updated_at ?? raw?.created_at;
  if (typeof value === "number") return value * 1000;
  if (typeof value !== "string") return NaN;
  return Date.parse(/T/.test(value) && !/(Z|[+-]\d\d:\d\d)$/.test(value) ? `${value}Z` : value);
}

export function WorkOverview({ sources, onOpen, onSchedules, otherChatCount = 0, onShowAll }: {
  sources: WorkItems;
  onOpen: (item: WorkItem) => void;
  onSchedules: () => void;
  otherChatCount?: number;
  onShowAll: () => void;
}) {
  const [showMore, setShowMore] = useState(false);
  const entries = Object.entries(sources) as [keyof WorkItems, WorkItems[keyof WorkItems]][];
  const items = entries.flatMap(([, source]) => source.items);
  const loading = entries.some(([, source]) => source.status === "loading");
  const disabled = items.filter((item) => item.kind === "schedule" && item.status === "paused");
  const older = items.filter((item) => laneFor(item.status) === "done" && !(finishedAt(item) >= Date.now() - 86_400_000));
  const visible = lanes.map((lane) => ({ ...lane, items: items.filter((item) =>
    laneFor(item.status) === lane.id && !disabled.includes(item)
    && (lane.id !== "done" || showMore || !older.includes(item))),
  })).filter((lane) => lane.items.length || (lane.id === "next" && disabled.length) || (lane.id === "done" && older.length));

  return <div className="work-overview">
    {entries.map(([kind, source]) => source.status === "error" ?
      <Alert key={kind} status="warning">Couldn&apos;t load {workSections[kind].toLowerCase()}. <Button size="sm" onClick={source.retry}>Retry</Button></Alert>
      : source.status === "stale" ? <Callout key={kind} tone="warning">{workSections[kind]} may be out of date. <Button size="sm" onClick={source.retry}>Retry</Button></Callout> : null)}
    {loading && <div aria-label="Loading work" aria-busy="true"><Skeleton lines={3} /><Skeleton lines={3} /></div>}
    {!loading && !visible.length && (otherChatCount ?
      <Empty title={`Nothing in this chat. ${otherChatCount} items in other chats.`} action={<Button onClick={onShowAll}>Show all chats</Button>} />
      : <Empty title="Nothing going on" />)}
    {visible.map((lane) => <section key={lane.id} aria-label={lane.label}>
      <h3>{lane.label}</h3>
      {visible.length === 1 && <p>{lane.help}</p>}
      {lane.items.map((item) => <WorkRow key={`${item.kind}:${item.id}`} {...item} focusKey={`${item.kind}:${item.id}`} onOpen={() => onOpen(item)} />)}
      {lane.id === "next" && disabled.length > 0 && <p>{disabled.length} disabled schedules · <Button variant="ghost" size="sm" onClick={onSchedules}>View</Button></p>}
      {lane.id === "done" && !showMore && older.length > 0 && <Button variant="ghost" size="sm" onClick={() => setShowMore(true)}>Show more</Button>}
    </section>)}
  </div>;
}
