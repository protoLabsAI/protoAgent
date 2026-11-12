import { useState } from "react";
import { Alert, Skeleton } from "@protolabsai/ui/data";
import { Button, Callout } from "@protolabsai/ui/primitives";
import { WorkEmpty as Empty } from "./WorkEmpty";
import { WorkHeading } from "./WorkHeading";
import { laneFor, type WorkLane } from "./tone";
import { WorkRow } from "./WorkRow";
import type { WorkItem, WorkItems } from "./useWorkItems";

export const workSections = {
  overview: "Overview", decisions: "Decisions", goals: "Goals", watches: "Watches",
  tasks: "Tasks", schedules: "Schedules", background: "Background agents",
  directives: "Directives",
};

/** Load-alert labels — one per SOURCE, not per section: the activity/inbox delivery source
 *  feeds the Needs-you group but has no section of its own since S4c removed Activity. */
const sourceLabels: Record<string, string> = { ...workSections, activity: "Activity" };

const lanes: { id: WorkLane; label: string; help: string }[] = [
  { id: "needs-you", label: "Needs you", help: "Decisions waiting for your answer." },
  { id: "doing", label: "Doing", help: "Running now." },
  { id: "waiting", label: "Waiting on", help: "Paused until something happens." },
  { id: "next", label: "Next", help: "Scheduled or queued." },
  { id: "done", label: "Done", help: "The last 24 hours." },
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

export function WorkOverview({ sources, onOpen, onSchedules }: {
  sources: WorkItems;
  onOpen: (item: WorkItem) => void;
  onSchedules: () => void;
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
      <Alert key={kind} status="warning">Couldn&apos;t load {sourceLabels[kind].toLowerCase()}. <Button size="sm" onClick={source.retry}>Retry</Button></Alert>
      : source.status === "stale" ? <Callout key={kind} tone="warning">{sourceLabels[kind]} may be out of date. <Button size="sm" onClick={source.retry}>Retry</Button></Callout> : null)}
    {loading && <div aria-label="Loading work" aria-busy="true"><Skeleton lines={3} /><Skeleton lines={3} /></div>}
    {!loading && !visible.length && <Empty title="Nothing going on" description="When the agent starts a goal, sets a watch, schedules something or asks you a question, it shows up here." />}
    {visible.map((lane) => <section key={lane.id} aria-label={lane.label}>
      <WorkHeading label={lane.label} count={lane.items.length} />
      {visible.length === 1 && <p>{lane.help}</p>}
      <ul className="work-group">{lane.items.map((item) => <li key={`${item.kind}:${item.id}`}><WorkRow {...item} focusKey={`${item.kind}:${item.id}`} onOpen={() => onOpen(item)} /></li>)}</ul>
      {lane.id === "next" && disabled.length > 0 && <p>{disabled.length} disabled schedules · <Button variant="ghost" size="sm" onClick={onSchedules}>View</Button></p>}
      {lane.id === "done" && !showMore && older.length > 0 && <Button variant="ghost" size="sm" onClick={() => setShowMore(true)}>Show more</Button>}
    </section>)}
  </div>;
}
