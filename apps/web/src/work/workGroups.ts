import type { DecisionRow } from "../lib/api/decisions";
import type { WorkItems } from "./useWorkItems";
import { decisionMeta, fromDecision, groupFor, type GroupableWorkItem, type WorkGroupId, type WorkJobRef } from "./tone";

export type WorkGroup = {
  id: WorkGroupId;
  label: string;
  items: GroupableWorkItem[];
  count: number;
};

export function jobEnded(item: GroupableWorkItem): boolean {
  const raw = item.raw as { status?: string } | null;
  switch (item.kind) {
    case "task": return raw?.status === "closed";
    case "background": return ["completed", "failed", "canceled"].includes(raw?.status ?? "");
    case "goal": return ["achieved", "exhausted", "unachievable", "cleared"].includes(raw?.status ?? "");
    default: return false;
  }
}

export const jobKey = (job: WorkJobRef) => JSON.stringify([job.kind, job.id]);

/** The selector's row universe for a `WorkItems` map — the one flatten the rail's live
 *  region shares with `workGroups`, so a transition it announces is a transition the
 *  counts actually took. */
export function flattenWorkItems(input: readonly GroupableWorkItem[] | WorkItems): readonly GroupableWorkItem[] {
  return Array.isArray(input) ? input : Object.values(input).flatMap((source) => source.groupItems ?? source.items);
}

/** The shared record to show over a source row, if any: a settled state is one-way, so a row
 *  the source already has as settled keeps it over a shared record still cached as pending. */
function sharedOverride(row: DecisionRow | undefined, shared: DecisionRow | undefined): DecisionRow | undefined {
  if (!shared) return undefined;
  return row && row.state !== "pending" && shared.state === "pending" ? undefined : shared;
}

/** Overlay the shared decisions store (useDecisions) onto the Work sources, so every
 *  consumer of the selector — the Work view AND the chat rail — counts an answer the moment
 *  it lands instead of waiting for the list refetch. Decision rows the store resolved as
 *  gone (404) are dropped; every other decision row shows its freshest record (a settled
 *  record always wins over a stale pending copy, see {@link sharedOverride}). */
export function withSharedDecisions(sources: WorkItems, shared: {
  records: Record<string, DecisionRow>;
  missing: Record<string, true>;
}): WorkItems {
  const keep = (item: { kind: string; id: string }) => item.kind !== "decision" || !shared.missing[item.id];
  const overlay = <T extends GroupableWorkItem>(item: T): T => {
    if (item.kind !== "decision") return item;
    const record = sharedOverride(item.raw as DecisionRow | undefined, shared.records[item.id]);
    return record
      ? ({ ...item, raw: record, status: fromDecision(record.state), meta: decisionMeta(record, true) } as T)
      : item;
  };
  return Object.fromEntries(Object.entries(sources).map(([key, source]) => [key, {
    ...source,
    items: source.items.filter(keep).map(overlay),
    ...(source.groupItems ? { groupItems: source.groupItems.filter(keep).map(overlay) } : {}),
  }])) as WorkItems;
}

export function inWorkScope(item: GroupableWorkItem, { thisChatOnly = false, sessionId }: {
  thisChatOnly?: boolean; sessionId?: string;
}): boolean {
  return groupFor(item) === "needs-you" || !thisChatOnly || (!!sessionId && item.sessionId === sessionId);
}

/** Shared row/count projection for Work and the rail. Supply ALL chats' rows
 * (useWorkItems with thisChatOnly: false), including terminal jobs. Missing jobs
 * are not proof of completion. No fetching, cache writes or server fulfilment.
 * Accepts the live source projection or standalone rows; legacy lane callers migrate in S3. */
export function workGroups(input: readonly GroupableWorkItem[] | WorkItems, {
  thisChatOnly = false,
  sessionId,
}: { thisChatOnly?: boolean; sessionId?: string } = {}): WorkGroup[] {
  const items = flattenWorkItems(input);
  const groups: WorkGroup[] = [
    { id: "needs-you", label: "Needs you", items: [], count: 0 },
    { id: "doing-now", label: "Doing now", items: [], count: 0 },
    { id: "coming-up", label: "Coming up", items: [], count: 0 },
    { id: "instructions", label: "Your instructions", items: [], count: 0 },
  ];
  const ended = new Set(items.filter(jobEnded).map((item) => jobKey(item as WorkJobRef)));
  for (const item of items) {
    const id = groupFor(item);
    if (!id) continue;
    if (item.kind === "directive" && item.tiedJob && ended.has(jobKey(item.tiedJob))) continue;
    if (!inWorkScope(item, { thisChatOnly, sessionId })) continue;
    const group = groups.find((candidate) => candidate.id === id)!;
    // A stopped goal needs attention without painting its failure as an error.
    group.items.push(item.kind === "goal" && id === "needs-you" ? { ...item, status: "paused" } : item);
  }
  for (const group of groups) group.count = group.items.length;
  return groups;
}
