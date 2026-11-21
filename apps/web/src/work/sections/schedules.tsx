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
 * Technical details. Actions: Edit first (§10.7 — the shared step-2 form, prefilled, saved
 * over the existing PUT), Pause / Resume (the bd-0ia51.8 enabled route) and Delete…, which
 * hands back an `undo_token` (bd-0ia51.5) that drives the §4.5 undo toast. Run now is
 * omitted: no run-now API exists.
 *
 * Work view additions §2 (#5543): a failing job shows a warning dot and its row meta ends
 * with the failure note; the detail gains a Health field and — for a Time follow-up, never
 * a wait — the Recent runs list (`JobRuns`), below the actions and above Technical details.
 * Every time in the detail is in the job's own clock (its `timezone`, else UTC) and names
 * the zone, which supersedes §2.8's "timezone only in Technical details".
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Skeleton, StatusDot } from "@protolabsai/ui/data";
import { Button, Callout } from "@protolabsai/ui/primitives";
import { WorkEmpty as Empty } from "../WorkEmpty";
import { useToast } from "@protolabsai/ui/overlays";

import { api } from "../../lib/api";
import { queryKeys } from "../../lib/queries";
import type { ScheduledJob } from "../../lib/types";
import { JobRuns } from "../JobRuns";
import { jobClockTime, jobZone, scheduleFailureNote, scheduleMeta, scheduleWhenInJobClock } from "../rowMeta";
import { WorkDetail, type WorkAction, type WorkField } from "../WorkDetail";
import { WorkEditDialog } from "../WorkCreateDialog";
import { draftFromSchedule } from "../workPersistence";
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

/** The detail's Health field (§2.2): "Failed the last 3 runs. Last worked Nov 16 14:00 UTC.",
 *  "Last worked today 14:00 UTC." or "Hasn't run yet.", with the last error under a failing
 *  job in muted text (two lines; the full text is in Technical details). Nothing is claimed
 *  for a job that has fired but whose outcomes were never recorded. */
function healthField(job: ScheduledJob, zone: string): WorkField["value"] {
  const note = scheduleFailureNote(job);
  const worked = job.last_ok ? jobClockTime(job.last_ok, zone, Date.now(), false) : "";
  const lastWorked = worked ? `Last worked ${worked}.` : "";
  if (!note) return lastWorked || (job.last_fire ? "" : "Hasn't run yet.");
  const failed = `${note[0].toUpperCase()}${note.slice(1)}.`;
  const error = (job.last_error ?? "").trim();
  return (
    <>
      {lastWorked ? `${failed} ${lastWorked}` : failed}
      {error && <span className="job-health__error">{error}</span>}
    </>
  );
}

function scheduleFields(job: ScheduledJob, status: WorkItem["status"], agentWide: boolean): WorkField[] {
  // One clock per detail (§2.3): every time below reads in the job's zone and names it.
  const zone = jobZone(job);
  const fields: WorkField[] = [{ label: "When", value: scheduleWhenInJobClock(job, true) }];
  if (job.next_fire && status !== "paused" && status !== "done") {
    fields.push({ label: "Next run", value: `${untilWords(job.next_fire)} (${jobClockTime(job.next_fire, zone, Date.now(), false)})` });
  }
  if (job.last_fire) fields.push({ label: "Last run", value: `Ran ${jobClockTime(job.last_fire, zone, Date.now(), false)}` });
  fields.push({ label: "Health", value: healthField(job, zone) });
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

  const paused = item.status === "paused";
  // Edit FIRST (§10.7): the same prefilled step-2 form the New flow confirms with — human
  // Repeat choices, never raw cron, no model call. Saving PUTs this job id in place.
  const actions: WorkAction[] = [
    { id: "edit", label: "Edit", run: async () => setEditOpen(true) },
    paused
      ? { id: "resume", label: "Resume", run: async () => { await api.setScheduleEnabled(job.id, true); refresh(); } }
      : { id: "pause", label: "Pause", run: async () => { await api.setScheduleEnabled(job.id, false); refresh(); } },
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
  if (job.last_error?.trim()) technical["Last error"] = job.last_error;

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
      >
        {/* A Time follow-up only (§2.1): a `wait:` job rides this detail but gets no list. */}
        {!job.id.startsWith("wait:") && <JobRuns key={job.id} job={job} />}
      </WorkDetail>
      {editOpen && (
        <WorkEditDialog
          draft={draftFromSchedule(job)}
          target={{ kind: "time", id: job.id, schedule: job.schedule, timezone: job.timezone ?? null }}
          onClose={() => setEditOpen(false)}
          onSaved={() => {
            refresh();
            toast({ tone: "success", message: "Schedule updated." });
          }}
        />
      )}
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
    return <Empty title="No schedules" description="The agent sets one when it needs to do something later." />;
  }
  return (
    <>
      {source.status === "stale" && (
        <Callout tone="warning">
          Showing the last load. Couldn&apos;t refresh.{" "}
          <Button size="sm" onClick={source.retry}>Retry</Button>
        </Callout>
      )}
      <ul className="work-group">
        {source.items.map((item) => (
          <li key={`${item.kind}:${item.id}`}>
            <WorkRow
              status={item.status}
              title={firstLine((item.raw as ScheduledJob).prompt) || item.title}
              meta={scheduleMeta(item.raw as ScheduledJob)}
              icon={scheduleFailureNote(item.raw as ScheduledJob) && item.status !== "paused"
                ? <StatusDot status="warning" /> : undefined}
              focusKey={`${item.kind}:${item.id}`}
              onOpen={() => onOpen(item)}
            />
          </li>
        ))}
      </ul>
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

registerWorkSection("schedules", { label: "Schedules", group: "coming-up", render: (ctx) => <SchedulesSection {...ctx} /> });
