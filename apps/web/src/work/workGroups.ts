import { groupFor, type GroupableWorkItem, type WorkGroupId, type WorkJobRef } from "./tone";

export type WorkGroup = {
  id: WorkGroupId;
  label: string;
  items: GroupableWorkItem[];
  count: number;
};

function jobEnded(item: GroupableWorkItem): boolean {
  const raw = item.raw as { status?: string } | null;
  switch (item.kind) {
    case "task": return raw?.status === "closed";
    case "background": return ["completed", "failed", "canceled"].includes(raw?.status ?? "");
    case "goal": return ["achieved", "exhausted", "unachievable", "cleared"].includes(raw?.status ?? "");
    default: return false;
  }
}

const jobKey = (job: WorkJobRef) => JSON.stringify([job.kind, job.id]);

/** Shared row/count projection for Work and the rail. Supply ALL chats' rows
 * (useWorkItems with thisChatOnly: false), including terminal jobs. Missing jobs
 * are not proof of completion. No fetching, cache writes or server fulfilment.
 * S2b supplies delivery/wait adapters; S3 migrates the existing lane callers. */
export function workGroups(items: readonly GroupableWorkItem[], {
  thisChatOnly = false,
  sessionId,
}: { thisChatOnly?: boolean; sessionId?: string } = {}): WorkGroup[] {
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
    if (id !== "needs-you" && thisChatOnly && (!sessionId || item.sessionId !== sessionId)) continue;
    const group = groups.find((candidate) => candidate.id === id)!;
    // A stopped goal needs attention without painting its failure as an error.
    group.items.push(item.kind === "goal" && id === "needs-you" ? { ...item, status: "paused" } : item);
  }
  for (const group of groups) group.count = group.items.length;
  return groups;
}
