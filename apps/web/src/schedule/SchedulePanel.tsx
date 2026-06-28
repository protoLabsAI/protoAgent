import "./schedule.css";

import { DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { ConfirmDialog, Dialog, useToast } from "@protolabsai/ui/overlays";
import {
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { CalendarClock, Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { StagePanel } from "../app/ErrorBoundary";
import { RefreshButton } from "../app/ui-kit";
import { PanelHeader, Tabs } from "@protolabsai/ui/navigation";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, schedulesQuery } from "../lib/queries";
import type { ScheduledJob } from "../lib/types";
import {
  buildOnce,
  buildRepeat,
  describeSchedule,
  WEEKDAYS,
  type RepeatFreq,
} from "./schedule-builder";

// Scheduled jobs (Activity → Schedule). The list is a useSuspenseQuery; add/cancel
// are useMutations that invalidate it. Adding is a friendly modal that builds the
// `schedule` string for you (a calendar for one-off, presets for recurring, raw cron
// as the escape hatch) — no hand-written cron required.

type Mode = "once" | "repeat" | "cron";

function ScheduleModal({
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
  const [mode, setMode] = useState<Mode>("once");
  const [onceAt, setOnceAt] = useState("");
  const [freq, setFreq] = useState<RepeatFreq>("daily");
  const [time, setTime] = useState("09:00");
  const [dow, setDow] = useState(1);
  const [cronRaw, setCronRaw] = useState("");
  const [prompt, setPrompt] = useState("");
  const [jobId, setJobId] = useState("");
  const [tz, setTz] = useState("");  // "" = UTC; only meaningful for recurring (cron)
  // Offer the operator's own zone + a few common ones; de-duped, browser zone first.
  const tzOptions = useMemo(() => {
    let local = "";
    try { local = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { /* ignore */ }
    const common = ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Tokyo"];
    return Array.from(new Set([local, ...common].filter(Boolean)));
  }, []);

  const schedule = useMemo(() => {
    if (mode === "once") return buildOnce(onceAt);
    if (mode === "repeat") return buildRepeat(freq, time, dow);
    return cronRaw.trim();
  }, [mode, onceAt, freq, time, dow, cronRaw]);

  const preview = describeSchedule(schedule);

  const canSubmit = !!prompt.trim() && !!schedule && !busy;

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
                  onClick={() => onAdd({ prompt: prompt.trim(), schedule, job_id: jobId.trim() || undefined, timezone: mode !== "once" && tz ? tz : undefined })}>
            <Plus size={16} /> Schedule
          </Button>
        </>
      }
    >
      <div className="schedule-form" data-testid="schedule-modal">
        <Tabs
          ariaLabel="Schedule mode"
          active={mode}
          onSelect={(m) => setMode(m as Mode)}
          items={[
            { id: "once", label: "Once" },
            { id: "repeat", label: "Repeat" },
            { id: "cron", label: "Cron" },
          ]}
        />

        {mode === "once" && (
          <label className="field">
            <span>Date &amp; time</span>
            <Input type="datetime-local" value={onceAt} onChange={(e) => setOnceAt(e.target.value)}
                   data-testid="schedule-once" />
          </label>
        )}

        {mode === "repeat" && (
          <div className="schedule-repeat">
            <label className="field">
              <span>Frequency</span>
              <DropdownSelect
                id="schedule-freq"
                value={freq}
                onValueChange={(v) => setFreq(v as RepeatFreq)}
                options={[
                  { value: "hourly", label: "Every hour" },
                  { value: "daily", label: "Every day" },
                  { value: "weekdays", label: "Every weekday (Mon–Fri)" },
                  { value: "weekly", label: "Every week" },
                ]}
              />
            </label>
            {freq === "weekly" && (
              <label className="field">
                <span>Day</span>
                <DropdownSelect
                  value={String(dow)}
                  onValueChange={(v) => setDow(Number(v))}
                  options={WEEKDAYS.map((d, i) => ({ value: String(i), label: d }))}
                />
              </label>
            )}
            <label className="field">
              <span>{freq === "hourly" ? "Minute" : "Time"}</span>
              <Input type="time" value={time} onChange={(e) => setTime(e.target.value)} data-testid="schedule-time" />
            </label>
          </div>
        )}

        {mode === "cron" && (
          <label className="field">
            <span>Cron expression (5 fields)</span>
            <Input value={cronRaw} onChange={(e) => setCronRaw(e.target.value)}
                   placeholder='e.g. "0 9 * * 1-5"' data-testid="schedule-cron" />
          </label>
        )}

        {mode !== "once" && (
          <label className="field">
            <span>Timezone</span>
            <DropdownSelect
              id="schedule-tz"
              value={tz}
              onValueChange={(v) => setTz(v)}
              options={[{ value: "", label: "UTC (default)" }, ...tzOptions.map((z) => ({ value: z, label: z }))]}
            />
          </label>
        )}

        <p className="schedule-preview" data-testid="schedule-preview">
          {preview ? <>Runs <strong>{preview}</strong> <code>{schedule}</code>{mode !== "once" && tz ? <span className="muted"> · {tz}</span> : null}</> : <span className="muted">Pick when it should run</span>}
        </p>

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
  const [schedule, setSchedule] = useState("");
  // Re-seed the editable fields whenever a different job is opened; always start in view mode.
  useEffect(() => {
    setPrompt(job?.prompt ?? "");
    setSchedule(job?.schedule ?? "");
    setEditing(false);
  }, [job]);

  if (!job) return null;
  const preview = describeSchedule(schedule);
  const dirty = prompt.trim() !== job.prompt || schedule.trim() !== job.schedule;
  const canSave = !!prompt.trim() && !!schedule.trim() && dirty && !busy;

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
              onClick={() => onSave(job.id, { prompt: prompt.trim(), schedule: schedule.trim(), timezone: job.timezone || undefined })}
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
                    onClick={() => setEditing(true)} disabled={busy}>
              <Pencil size={16} /> Edit
            </Button>
          </>
        )
      }
    >
      <div className="schedule-form" data-testid="schedule-detail">
        {editing ? (
          <>
            <label className="field">
              <span>When it runs (cron expression, or an ISO date for one-off)</span>
              <Input value={schedule} onChange={(e) => setSchedule(e.target.value)}
                     data-testid="schedule-detail-schedule" />
            </label>
            <p className="schedule-preview">
              {preview
                ? <>Runs <strong>{preview}</strong> <code>{schedule}</code>{job.timezone ? <span className="muted"> · {job.timezone}</span> : null}</>
                : <span className="muted">Enter a cron expression or an ISO date</span>}
            </p>
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
  const cancel = useMutation({ mutationFn: (id: string) => api.cancelSchedule(id), onSettled: invalidate });
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
