/**
 * Work view ▸ Schedules section (#4955 §2.8).
 *
 * Registers itself on import. One component renders both panes of the Work view: the list
 * of `WorkRow`s (when no item is open) and the `WorkDetail` for the open item — the two are
 * told apart by `state.itemId`, exactly as the shell drives them (WorkView §3.1).
 *
 * A row's title is the prompt's first line, never the job id; "When" is plain words built
 * from the schedule-builder's `parseSchedule`, never raw cron. Everything on §2.8's
 * "Never shown" list — the id, the cron string and the IANA timezone — lives only in
 * Technical details. Actions: Pause / Resume (the bd-0ia51.8 enabled route), Edit (the
 * existing PUT, reusing `ScheduleModal`) and Delete…, which hands back an `undo_token`
 * (bd-0ia51.5) that drives the §4.5 undo toast. Run now is omitted: no run-now API exists.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Skeleton } from "@protolabsai/ui/data";
import { Button, Callout, Empty } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";

import { api } from "../../lib/api";
import { queryKeys } from "../../lib/queries";
import type { ScheduledJob } from "../../lib/types";
import { ScheduleModal } from "../../schedule/SchedulePanel";
import { localZone, parseSchedule } from "../../schedule/schedule-builder";
import { relTime } from "../tone";
import { scheduleWhen } from "../rowMeta";
import { WorkDetail, type WorkAction, type WorkField } from "../WorkDetail";
import { WorkRow } from "../WorkRow";
import { registerWorkSection, type WorkSectionContext } from "../sectionRegistry";
import type { WorkItem, WorkSource } from "../useWorkItems";

/** The prompt's first line — the row/detail title. Never the job id (§2.8 field 1). */
function firstLine(text: string): string {
  return (text || "").split("\n")[0].trim();
}

/** Forward-relative phrase for a future fire ("in 2 h"), paired with `relTime` for the clock. */
function untilWords(iso: string, now: number = Date.now()): string {
  const at = new Date(iso).getTime();
  if (!Number.isFinite(at)) return "";
  const minutes = Math.round((at - now) / 60_000);
  if (minutes <= 0) return "now";
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `in ${hours} h`;
  const days = Math.round(hours / 24);
  return `in ${days} day${days === 1 ? "" : "s"}`;
}

/** "7 Nov" — a date with no clock, for the Created line (§2.8 field 7). */
function dateShort(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function scheduleFields(job: ScheduledJob, status: WorkItem["status"], agentWide: boolean): WorkField[] {
  const zone = job.timezone && job.timezone !== localZone() ? ` (${job.timezone})` : "";
  const fields: WorkField[] = [{ label: "When", value: scheduleWhen(job.schedule) + zone }];
  if (job.next_fire && status !== "paused" && status !== "done") {
    fields.push({ label: "Next run", value: `${untilWords(job.next_fire)} (${relTime(job.next_fire).text})` });
  }
  if (job.last_fire) fields.push({ label: "Last run", value: `Ran ${relTime(job.last_fire).text}` });
  if (agentWide && job.agent_name) fields.push({ label: "Agent", value: job.agent_name });
  if (job.created_at) fields.push({ label: "Created", value: dateShort(job.created_at) });
  return fields;
}

function ScheduleDetail({ item, state }: { item: WorkItem; state: WorkSectionContext["state"] }) {
  const job = item.raw as ScheduledJob;
  const qc = useQueryClient();
  const toast = useToast();
  const [editOpen, setEditOpen] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: queryKeys.schedules });

  const edit = useMutation({
    mutationFn: (body: { prompt: string; schedule: string; timezone: string | null }) =>
      api.updateSchedule(job.id, body),
    onSuccess: () => {
      refresh();
      setEditOpen(false);
    },
    onError: () => toast({ tone: "warning", message: "Couldn't save the schedule." }),
  });

  const paused = item.status === "paused";
  const actions: WorkAction[] = [
    paused
      ? { id: "resume", label: "Resume", run: async () => { await api.setScheduleEnabled(job.id, true); refresh(); } }
      : { id: "pause", label: "Pause", run: async () => { await api.setScheduleEnabled(job.id, false); refresh(); } },
    { id: "edit", label: "Edit", run: async () => setEditOpen(true) },
    {
      id: "delete",
      label: "Delete…",
      destructive: true,
      confirm: {
        prompt: "Delete this schedule?",
        explanation: "It won't run again. You can undo this for a few seconds.",
        confirmLabel: "Delete",
        keepLabel: "Keep it",
      },
      run: async () => {
        // The bd-0ia51.5 delete mints a short-lived `undo_token` the API type doesn't
        // carry yet; read it off the response to drive the §4.5 undo toast.
        const res = (await api.cancelSchedule(job.id)) as { undo_token?: string };
        refresh();
        return { undoToken: res.undo_token };
      },
    },
  ];

  const technical: Record<string, string> = { Id: job.id, "Schedule (cron)": job.schedule };
  if (job.timezone) technical["Timezone (IANA)"] = job.timezone;

  return (
    <>
      <WorkDetail
        title={firstLine(job.prompt) || "Schedule"}
        status={item.status}
        fields={scheduleFields(job, item.status, true)}
        actions={actions}
        technical={technical}
        type="Schedule"
        onUndo={async (token) => { await api.undo(token); refresh(); }}
      />
      <ScheduleModal
        open={editOpen}
        onClose={() => setEditOpen(false)}
        busy={edit.isPending}
        title="Edit schedule"
        submitLabel="Save changes"
        initial={{ prompt: job.prompt, schedule: job.schedule, parsed: parseSchedule(job.schedule), timezone: job.timezone ?? "" }}
        onAdd={(body) => edit.mutate({ prompt: body.prompt, schedule: body.schedule, timezone: body.timezone ?? null })}
      />
    </>
  );
}

function ScheduleList({ source, onOpen }: { source: WorkSource; onOpen: (item: WorkItem) => void }) {
  if (source.status === "loading") {
    return <div aria-busy="true" aria-label="Loading schedules"><Skeleton lines={3} /></div>;
  }
  if (source.status === "error") {
    return (
      <Alert status="warning">
        Couldn&apos;t load schedules. The agent still has them. Only this view failed.{" "}
        <Button size="sm" onClick={source.retry}>Retry</Button>
      </Alert>
    );
  }
  if (!source.items.length) {
    return <Empty title="No schedules. The agent sets one when it needs to do something later." />;
  }
  return (
    <>
      {source.status === "stale" && (
        <Callout tone="warning">
          Showing the last load. Couldn&apos;t refresh.{" "}
          <Button size="sm" onClick={source.retry}>Retry</Button>
        </Callout>
      )}
      {source.items.map((item) => (
        <WorkRow
          key={`${item.kind}:${item.id}`}
          status={item.status}
          title={firstLine((item.raw as ScheduledJob).prompt) || item.title}
          meta={scheduleWhen((item.raw as ScheduledJob).schedule)}
          focusKey={`${item.kind}:${item.id}`}
          onOpen={() => onOpen(item)}
        />
      ))}
    </>
  );
}

/** One component for both panes — `state.itemId` selects detail vs list (WorkView §3.1). */
export function SchedulesSection({ sources, state, onOpen }: WorkSectionContext) {
  const source = sources.schedules;
  if (state.itemId) {
    const item = source.items.find(
      (candidate) => `${candidate.kind}:${candidate.id}` === state.itemId || candidate.id === state.itemId,
    );
    return item ? <ScheduleDetail item={item} state={state} /> : null;
  }
  return <ScheduleList source={source} onOpen={onOpen} />;
}

registerWorkSection("schedules", { label: "Schedules", render: (ctx) => <SchedulesSection {...ctx} /> });
