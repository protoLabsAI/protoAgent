/**
 * Work's four approved groups (work-consolidation.md v2, ADR 0129 #5100): the Overview
 * body, the picked-group body and the one row presentation they share. Rows and counts
 * come from the shared `workGroups` selector — never from per-source filters — so Work,
 * the picker badges and the chat status rail can't drift apart.
 */
import type { ReactNode } from "react";
import { Alert, Skeleton, Spinner, StatusDot } from "@protolabsai/ui/data";
import { Button, Callout, type Status } from "@protolabsai/ui/primitives";
import { CircleDashed, Clock, Eye, Target } from "lucide-react";
import { WorkEmpty as Empty } from "./WorkEmpty";
import { WorkHeading } from "./WorkHeading";
import { decisionMeta, relTime, type GroupableWorkItem, type WorkGroupId } from "./tone";
import type { DecisionRow } from "../lib/api/decisions";
import type { WorkGroup } from "./workGroups";
import { WorkRow } from "./WorkRow";
import type { WorkItems, WorkSourceKind } from "./useWorkItems";

/** Source display names for the per-source failure notices — a failed SOURCE is named by
 *  what failed ("Couldn't load decisions"), never by the public group it feeds. */
export const workSections = {
  overview: "Overview", decisions: "Decisions", goals: "Goals", watches: "Watches",
  tasks: "Tasks", schedules: "Schedules", background: "Background agents",
  directives: "Directives",
};

/** Load-alert labels — one per SOURCE, not per section: the activity/inbox delivery source
 *  feeds the Needs-you group but has no section of its own since S4c removed Activity. */
const sourceLabels: Record<string, string> = { ...workSections, activity: "Activity" };

/** The group help lines, verbatim from the approved spec (§1). Shown on a picked group. */
export const groupHelp: Record<WorkGroupId, string> = {
  "needs-you": "Waiting on you. Nothing else in Work asks for your attention.",
  "doing-now": "Running now: background jobs and the active goal.",
  "coming-up": "Not started yet: runs at a time, when something happens, or when it's picked up.",
  instructions: "Your standing instructions and approvals, in force now.",
};

/** A picked group's inline empty text (§4). */
export const groupEmpty: Record<WorkGroupId, string> = {
  "needs-you": "Nothing needs you.",
  "doing-now": "Nothing running.",
  "coming-up": "Nothing planned.",
  instructions: "No standing instructions.",
};

/** Which sources feed each group (groupFor in ./tone), for loading/failure scoping. */
export const groupSources: Record<WorkGroupId, WorkSourceKind[]> = {
  "needs-you": ["decisions", "goals", "activity"],
  "doing-now": ["goals", "background", "tasks"],
  "coming-up": ["background", "tasks", "watches", "schedules"],
  instructions: ["directives"],
};

/** The selection/focus key for a row. A `wait` rides the schedules source, so it keys as
 *  that schedule — opening it lands on the existing schedule detail renderer. */
export function itemKey(item: Pick<GroupableWorkItem, "kind" | "id">): string {
  return `${item.kind === "wait" ? "schedule" : item.kind}:${item.id}`;
}

/** "not started · added 3 min ago" — a Coming up job that hasn't begun (§1). */
function notStartedMeta(item: GroupableWorkItem): string {
  const raw = item.raw as { created_at?: string } | null;
  const added = raw?.created_at ? relTime(raw.created_at).text : "";
  return added ? `not started · added ${added}` : "not started";
}

/** The §2 row table: the kind tag, its tone and the leading icon. Only Needs you carries
 *  an accent (decision info / delivery warning); a stopped goal stays neutral, never error. */
function rowPresentation(item: GroupableWorkItem, group: WorkGroupId): {
  tag: string; tone: Status; icon: ReactNode; meta?: string;
} {
  switch (group) {
    case "needs-you":
      // Work lists every chat, so a decision row keeps its agent name (§2.4 field 5).
      if (item.kind === "decision") return { tag: "Decision", tone: "info", icon: <StatusDot status="info" />,
        meta: decisionMeta(item.raw as DecisionRow, true) };
      if (item.kind === "goal") return { tag: "Goal stopped", tone: "neutral", icon: <StatusDot status="neutral" /> };
      return { tag: "Delivery", tone: "warning", icon: <StatusDot status="warning" /> };
    case "doing-now":
      if (item.kind === "goal") return { tag: "Goal", tone: "neutral", icon: <Target size={16} /> };
      return { tag: "Job", tone: "neutral", icon: <Spinner size={16} /> };
    case "coming-up":
      if (item.kind === "watch") return { tag: "Condition", tone: "neutral", icon: <Eye size={16} /> };
      if (item.kind === "schedule" || item.kind === "wait") return { tag: "Time", tone: "neutral", icon: <Clock size={16} /> };
      return { tag: "Job", tone: "neutral", icon: <CircleDashed size={16} />, meta: notStartedMeta(item) };
    case "instructions":
      return { tag: "Instruction", tone: "neutral", icon: <StatusDot status="info" /> };
  }
}

/** One bordered list of a group's rows — the same markup in the Overview and a picked group. */
export function WorkGroupRows({ group, onOpen }: {
  group: WorkGroup;
  onOpen: (item: GroupableWorkItem) => void;
}) {
  return (
    <ul className="work-group">
      {group.items.map((item) => {
        const row = rowPresentation(item, group.id);
        return (
          <li key={itemKey(item)}>
            <WorkRow
              status={item.status}
              title={item.title}
              meta={row.meta ?? item.meta}
              tag={row.tag}
              tagTone={row.tone}
              icon={row.icon}
              focusKey={itemKey(item)}
              onOpen={() => onOpen(item)}
            />
          </li>
        );
      })}
    </ul>
  );
}

/** The per-source failure notices: an error keeps its source's own words; stale keeps the
 *  retained rows visible under a refresh warning. `kinds` scopes a picked group's notices
 *  to the sources that actually feed it. */
function SourceNotices({ sources, kinds }: { sources: WorkItems; kinds?: readonly WorkSourceKind[] }) {
  const entries = (Object.entries(sources) as [WorkSourceKind, WorkItems[WorkSourceKind]][])
    .filter(([kind]) => !kinds || kinds.includes(kind));
  return <>
    {entries.map(([kind, source]) => source.status === "error" ?
      <Alert key={kind} status="warning">Couldn&apos;t load {sourceLabels[kind].toLowerCase()}. <Button size="sm" onClick={source.retry}>Retry</Button></Alert>
      : source.status === "stale" ? <Callout key={kind} tone="warning">{sourceLabels[kind]} may be out of date. <Button size="sm" onClick={source.retry}>Retry</Button></Callout> : null)}
  </>;
}

/** A picked group: its help line, then its rows or its inline empty text (§4). The Your
 *  instructions group passes `body` — the registered directives renderer — so the §3
 *  presentation (Approval tags, provenance, its own load/error copy) rides the group. */
export function WorkGroupSection({ group, sources, onOpen, body }: {
  group: WorkGroup;
  sources: WorkItems;
  onOpen: (item: GroupableWorkItem) => void;
  body?: ReactNode;
}) {
  const kinds = groupSources[group.id];
  const loading = kinds.some((kind) => sources[kind].status === "loading");
  // A failed source already explains itself in the notice above — don't also claim
  // "nothing here" when the rows may simply not have loaded.
  const failed = kinds.some((kind) => sources[kind].status === "error");
  return <div className="work-overview">
    <p className="work-view__help">{groupHelp[group.id]}</p>
    {body ?? <>
      <SourceNotices sources={sources} kinds={kinds} />
      {loading ? <div aria-label="Loading work" aria-busy="true"><Skeleton lines={3} /></div>
        : group.items.length ? <WorkGroupRows group={group} onOpen={onOpen} />
          : failed ? null : <Empty title={groupEmpty[group.id]} />}
    </>}
  </div>;
}

/** The Overview: the four groups in spec order, empty groups hidden (§4). */
export function WorkOverview({ groups, sources, onOpen }: {
  groups: WorkGroup[];
  sources: WorkItems;
  onOpen: (item: GroupableWorkItem) => void;
}) {
  const loading = Object.values(sources).some((source) => source.status === "loading");
  const failed = Object.values(sources).some((source) => source.status === "error");
  const visible = groups.filter((group) => group.items.length > 0);
  return <div className="work-overview">
    <SourceNotices sources={sources} />
    {loading && <div aria-label="Loading work" aria-busy="true"><Skeleton lines={3} /><Skeleton lines={3} /></div>}
    {!loading && !visible.length && !failed && <Empty title="Nothing to track."
      description="Nothing needs you, nothing is running or planned, and you haven't given standing instructions." />}
    {visible.map((group) => <section key={group.id} aria-label={group.label}>
      <WorkHeading label={group.label} count={group.count} />
      <WorkGroupRows group={group} onOpen={onOpen} />
    </section>)}
  </div>;
}
