import "./schedule.css";

import { Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { ConfirmDialog, Dialog, useToast } from "@protolabsai/ui/overlays";
import {
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { CalendarClock, Pencil, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { ScheduleBuilder, type BuilderOut } from "./ScheduleBuilder";

import { StagePanel } from "../app/ErrorBoundary";
import { RefreshButton } from "../app/ui-kit";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, schedulesQuery } from "../lib/queries";
import type { ScheduledJob } from "../lib/types";
import { canonicalSchedule, describeSchedule, localZone, parseSchedule } from "./schedule-builder";

// Scheduled jobs (Activity → Schedule). The list is a useSuspenseQuery; add/cancel
// are useMutations that invalidate it. Adding is a friendly modal that builds the
// `schedule` string for you (a calendar for one-off, presets for recurring, raw cron
// as the escape hatch) — no hand-written cron required.

// New schedules default to the operator's local zone (#2159 part 5); the edit dialog
// instead seeds the job's STORED zone so editing never silently retimes it.
const NEW_INITIAL = { parsed: { mode: "once" as const, onceDate: "", onceTime: "" }, timezone: localZone() };

// Exported so the Work overview's Schedule-card quick-add reuses it (that host owns its
// own open-state + add mutation) — one form, two hosts.
export function ScheduleModal({
  open,
  onClose,
  onAdd,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  onAdd: (body: { prompt: string; schedule: string; job_id?: string; timezone?: string }) => void;
  busy: boolean;
}) {
  const [prompt, setPrompt] = useState("");
  const [jobId, setJobId] = useState("");
  const [out, setOut] = useState<BuilderOut>({ schedule: "", valid: false, error: "" });
  const onBuilderChange = useCallback((o: BuilderOut) => setOut(o), []);
  // Reset to a clean slate each time the modal opens (the builder remounts via `key`).
  useEffect(() => {
    if (open) {
      setPrompt("");
      setJobId("");
      setOut({ schedule: "", valid: false, error: "" });
    }
  }, [open]);

  const canSubmit = !!prompt.trim() && out.valid && !busy;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={<><CalendarClock size={16} /> New schedule</>}
      width="min(560px, 94vw)"
      className="schedule-dialog"
      footer={
        <>
          <Button type="button" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="primary" disabled={!canSubmit} data-testid="schedule-submit"
                  onClick={() => onAdd({ prompt: prompt.trim(), schedule: out.schedule, job_id: jobId.trim() || undefined, timezone: out.timezone })}>
            <Plus size={16} /> Schedule
          </Button>
        </>
      }
    >
      <div className="schedule-form" data-testid="schedule-modal">
        <ScheduleBuilder key={open ? "open" : "closed"} initial={NEW_INITIAL} onChange={onBuilderChange} />

        <label className="field">
          <span>Prompt (delivered to the agent when it fires)</span>
          <Textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={4}
                    placeholder="What the agent should do when this fires" data-testid="schedule-prompt" />
        </label>
        <label className="field">
          <span>Job id (optional)</span>
          <Input value={jobId} onChange={(e) => setJobId(e.target.value)} placeholder="auto" />
        </label>

      </div>
    </Dialog>
  );
}

// Click a job row to open this — read the FULL prompt + every field, or flip to Edit
// to change the prompt / schedule. The backend has no in-place update (add errors on a
// duplicate id), so Save is a cancel-then-re-add of the same id (done by the parent).
function ScheduleDetailDialog({
  job,
  onClose,
  onSave,
  onDelete,
  busy,
}: {
  job: ScheduledJob | null;
  onClose: () => void;
  onSave: (id: string, body: { prompt: string; schedule: string; timezone?: string }) => void;
  onDelete: (id: string) => void;
  busy: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [out, setOut] = useState<BuilderOut>({ schedule: "", valid: false, error: "" });
  const onBuilderChange = useCallback((o: BuilderOut) => setOut(o), []);
  // Re-seed whenever a different job is opened; always start in view mode.
  useEffect(() => {
    setPrompt(job?.prompt ?? "");
    setEditing(false);
  }, [job]);

  if (!job) return null;
  // The edit builder is seeded by parsing the stored schedule into its mode (#2159 part 4).
  // Dirty compares against the builder's CANONICAL form of the stored string, so a job the
  // builder merely re-formats (a leading-zero cron hour) doesn't open dirty — and the
  // timezone counts too ("" / undefined both mean UTC), so a zone-only change is savable.
  const scheduleChanged = out.schedule.trim() !== canonicalSchedule(job.schedule);
  const timezoneChanged = (out.timezone ?? "") !== (job.timezone ?? "");
  const dirty = prompt.trim() !== job.prompt || scheduleChanged || timezoneChanged;
  // Builder validity only gates a save that CHANGES the schedule — an untouched one saves
  // the stored string verbatim, so a job whose stored schedule our validator would reject
  // (a past one-off, a legacy cron) can still take prompt/timezone edits.
  const canSave = !!prompt.trim() && (out.valid || !scheduleChanged) && dirty && !busy;

  return (
    <Dialog
      open={!!job}
      onClose={onClose}
      title={<><CalendarClock size={16} /> {editing ? "Edit schedule" : "Scheduled job"}</>}
      width="min(560px, 94vw)"
      className="schedule-dialog"
      footer={
        editing ? (
          <>
            <Button type="button" onClick={() => setEditing(false)} disabled={busy}>Cancel</Button>
            <Button
              type="button"
              variant="primary"
              disabled={!canSave}
              data-testid="schedule-detail-save"
              // An untouched schedule keeps the STORED string (not the canonicalized
              // rebuild), so a prompt- or zone-only save never rewrites it.
              onClick={() => onSave(job.id, {
                prompt: prompt.trim(),
                schedule: scheduleChanged ? out.schedule : job.schedule,
                timezone: out.timezone,
              })}
            >
              Save changes
            </Button>
          </>
        ) : (
          <>
            <Button type="button" onClick={onClose}>Close</Button>
            <Button type="button" variant="ghost" data-testid="schedule-detail-delete"
                    onClick={() => onDelete(job.id)} disabled={busy} title="Delete job">
              <Trash2 size={16} /> Delete
            </Button>
            <Button type="button" variant="primary" data-testid="schedule-detail-edit"
                    // Re-seed on ENTRY so a previously canceled edit can't leak back in:
                    // the prompt state survives Cancel (the job didn't change), and the
                    // builder remounts fresh from the job anyway.
                    onClick={() => { setPrompt(job.prompt); setEditing(true); }} disabled={busy}>
              <Pencil size={16} /> Edit
            </Button>
          </>
        )
      }
    >
      <div className="schedule-form" data-testid="schedule-detail">
        {editing ? (
          <>
            <ScheduleBuilder
              key={job.id}
              initial={{ parsed: parseSchedule(job.schedule), timezone: job.timezone ?? "" }}
              onChange={onBuilderChange}
            />
            <label className="field">
              <span>Prompt (delivered to the agent when it fires)</span>
              <Textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={6}
                        data-testid="schedule-detail-prompt" />
            </label>
          </>
        ) : (
          <dl className="schedule-detail-grid">
            <dt>Schedule</dt>
            <dd>
              {describeSchedule(job.schedule)} <code>{job.schedule}</code>
              {job.timezone ? <span className="muted"> · {job.timezone}</span> : null}
            </dd>
            {job.next_fire ? (<><dt>Next fire</dt><dd>{job.next_fire}</dd></>) : null}
            {job.last_fire ? (<><dt>Last fire</dt><dd>{job.last_fire}</dd></>) : null}
            {job.created_at ? (<><dt>Created</dt><dd>{job.created_at}</dd></>) : null}
            <dt>Job id</dt>
            <dd><code>{job.id}</code></dd>
            <dt>Prompt</dt>
            <dd className="schedule-detail-promptbody" data-testid="schedule-detail-promptbody">{job.prompt}</dd>
          </dl>
        )}
      </div>
    </Dialog>
  );
}

function ScheduleBody() {
  const queryClient = useQueryClient();
  const { data, isFetching, refetch } = useSuspenseQuery(schedulesQuery());
  const jobs = data.jobs;
  const backend = data.backend;
  const [modalOpen, setModalOpen] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  // Track jobs by id so they follow live refetches (and the dialogs close if deleted).
  const detailJob = jobs.find((j) => j.id === detailId) ?? null;
  const confirmJob = jobs.find((j) => j.id === confirmDeleteId) ?? null;

  const invalidate = () => queryClient.invalidateQueries({ queryKey: queryKeys.schedules });
  // Transient action feedback is a TOAST, not an inline line — the in-progress state
  // already shows on each button's disabled/pending affordance.
  const toast = useToast();

  const add = useMutation({
    mutationFn: (body: { prompt: string; schedule: string; job_id?: string; timezone?: string }) => api.addSchedule(body),
    onSuccess: () => {
      setModalOpen(false);
      toast({ tone: "success", title: "Scheduled", message: "The job was added." });
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't schedule", message: errMsg(e) }),
    onSettled: invalidate,
  });
  // Delete gets the same toast feedback as add/edit — without onError a failed delete
  // looks successful, because the confirm dialog closes without waiting on the mutation.
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelSchedule(id),
    onSuccess: () => toast({ tone: "success", title: "Job deleted", message: "It won't fire again." }),
    onError: (e) => toast({ tone: "error", title: "Couldn't delete the job", message: errMsg(e) }),
    onSettled: invalidate,
  });
  // Atomic in-place edit (PUT) — id / created_at / last_fire preserved, next_fire
  // recomputed server-side; a bad schedule 400s without touching the job.
  const edit = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { prompt: string; schedule: string; timezone?: string } }) =>
      api.updateSchedule(id, body),
    onSuccess: () => {
      setDetailId(null);
      toast({ tone: "success", title: "Schedule updated", message: "Your changes were saved." });
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't save the job", message: errMsg(e) }),
    onSettled: invalidate,
  });
  const busy = add.isPending || cancel.isPending || edit.isPending;

  return (
    <>
      <PanelHeader
        title="Schedule"
        kicker={`${jobs.length} job${jobs.length === 1 ? "" : "s"} · ${backend}`}
        actions={
          <>
            <RefreshButton onClick={() => void refetch()} busy={isFetching} />
            <Button variant="primary" type="button" onClick={() => setModalOpen(true)}
                    disabled={backend === "disabled"} data-testid="schedule-new">
              <Plus size={16} /> New schedule
            </Button>
          </>
        }
      />

      <div className="stage-body">
        <div className="subagent-list">
          {jobs.length ? (
            jobs.map((job) => (
              <div className="subagent-row" key={job.id}>
                <button type="button" className="schedule-row-open" onClick={() => setDetailId(job.id)}
                        data-testid={`schedule-row-${job.id}`} title="Open details">
                  <strong>{job.id}</strong>
                  <span>
                    {describeSchedule(job.schedule)}
                    {job.next_fire ? ` · next ${job.next_fire}` : ""}
                    {" · "}
                    {job.prompt.length > 80 ? `${job.prompt.slice(0, 80)}…` : job.prompt}
                  </span>
                </button>
                <Button icon variant="ghost" type="button" onClick={() => setConfirmDeleteId(job.id)}
                        disabled={busy} title="Delete job">
                  <Trash2 size={16} />
                </Button>
              </div>
            ))
          ) : (
            <div className="subagent-row">
              <div>
                <strong>No scheduled jobs</strong>
                <span>{backend !== "local" && backend !== "disabled" ? `jobs may be managed remotely by ${backend}` : "create one with “New schedule”"}</span>
              </div>
            </div>
          )}
        </div>
      </div>

      <ScheduleModal open={modalOpen} onClose={() => setModalOpen(false)} onAdd={(b) => add.mutate(b)} busy={busy} />
      <ScheduleDetailDialog
        job={detailJob}
        onClose={() => { setDetailId(null); edit.reset(); }}
        onSave={(id, body) => edit.mutate({ id, body })}
        onDelete={(id) => setConfirmDeleteId(id)}
        busy={busy}
      />
      <ConfirmDialog
        open={confirmDeleteId !== null}
        title="Delete scheduled job?"
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          if (confirmDeleteId) cancel.mutate(confirmDeleteId, { onSuccess: () => setDetailId(null) });
          setConfirmDeleteId(null);
        }}
        onClose={() => setConfirmDeleteId(null)}
      >
        {confirmJob
          ? `"${describeSchedule(confirmJob.schedule)}" — ${confirmJob.prompt.length > 80 ? `${confirmJob.prompt.slice(0, 80)}…` : confirmJob.prompt}. It will stop firing and be removed. This can't be undone.`
          : undefined}
      </ConfirmDialog>
    </>
  );
}

export function SchedulePanel() {
  return (
    <StagePanel label="schedule">
      <ScheduleBody />
    </StagePanel>
  );
}
