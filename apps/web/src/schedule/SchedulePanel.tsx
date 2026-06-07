import {
  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { CalendarClock, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { Suspense, useEffect, useMemo, useState } from "react";

import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { PanelHeader } from "../app/PanelHeader";
import { api } from "../lib/api";
import { queryKeys, schedulesQuery } from "../lib/queries";
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
  onAdd: (body: { prompt: string; schedule: string; job_id?: string }) => void;
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

  const schedule = useMemo(() => {
    if (mode === "once") return buildOnce(onceAt);
    if (mode === "repeat") return buildRepeat(freq, time, dow);
    return cronRaw.trim();
  }, [mode, onceAt, freq, time, dow, cronRaw]);

  const preview = describeSchedule(schedule);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const canSubmit = !!prompt.trim() && !!schedule && !busy;

  return (
    <div className="confirm-overlay" role="dialog" aria-modal="true" aria-label="New schedule"
         onClick={onClose} data-testid="schedule-modal">
      <div className="confirm-card schedule-card" onClick={(e) => e.stopPropagation()}>
        <div className="confirm-head">
          <CalendarClock size={16} />
          <h2>New schedule</h2>
          <button className="icon-button schedule-close" type="button" onClick={onClose} title="Close">
            <X size={16} />
          </button>
        </div>

        <div className="schedule-modes" role="tablist">
          <button type="button" role="tab" aria-selected={mode === "once"}
                  className={mode === "once" ? "active" : ""} onClick={() => setMode("once")}>Once</button>
          <button type="button" role="tab" aria-selected={mode === "repeat"}
                  className={mode === "repeat" ? "active" : ""} onClick={() => setMode("repeat")}>Repeat</button>
          <button type="button" role="tab" aria-selected={mode === "cron"}
                  className={mode === "cron" ? "active" : ""} onClick={() => setMode("cron")}>Cron</button>
        </div>

        {mode === "once" && (
          <label className="field">
            <span>Date &amp; time</span>
            <input type="datetime-local" value={onceAt} onChange={(e) => setOnceAt(e.target.value)}
                   data-testid="schedule-once" />
          </label>
        )}

        {mode === "repeat" && (
          <div className="schedule-repeat">
            <label className="field">
              <span>Frequency</span>
              <select value={freq} onChange={(e) => setFreq(e.target.value as RepeatFreq)} data-testid="schedule-freq">
                <option value="hourly">Every hour</option>
                <option value="daily">Every day</option>
                <option value="weekdays">Every weekday (Mon–Fri)</option>
                <option value="weekly">Every week</option>
              </select>
            </label>
            {freq === "weekly" && (
              <label className="field">
                <span>Day</span>
                <select value={dow} onChange={(e) => setDow(Number(e.target.value))}>
                  {WEEKDAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
                </select>
              </label>
            )}
            <label className="field">
              <span>{freq === "hourly" ? "Minute" : "Time"}</span>
              <input type="time" value={time} onChange={(e) => setTime(e.target.value)} data-testid="schedule-time" />
            </label>
          </div>
        )}

        {mode === "cron" && (
          <label className="field">
            <span>Cron expression (5 fields)</span>
            <input value={cronRaw} onChange={(e) => setCronRaw(e.target.value)}
                   placeholder='e.g. "0 9 * * 1-5"' data-testid="schedule-cron" />
          </label>
        )}

        <p className="schedule-preview" data-testid="schedule-preview">
          {preview ? <>Runs <strong>{preview}</strong> <code>{schedule}</code></> : <span className="muted">Pick when it should run</span>}
        </p>

        <label className="field">
          <span>Prompt (delivered to the agent when it fires)</span>
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={4}
                    placeholder="What the agent should do when this fires" data-testid="schedule-prompt" />
        </label>
        <label className="field">
          <span>Job id (optional)</span>
          <input value={jobId} onChange={(e) => setJobId(e.target.value)} placeholder="auto" />
        </label>

        <div className="confirm-actions">
          <button type="button" className="secondary-button" onClick={onClose}>Cancel</button>
          <button type="button" className="primary-button" disabled={!canSubmit} data-testid="schedule-submit"
                  onClick={() => onAdd({ prompt: prompt.trim(), schedule, job_id: jobId.trim() || undefined })}>
            <Plus size={16} /> Schedule
          </button>
        </div>
      </div>
    </div>
  );
}

function ScheduleBody() {
  const queryClient = useQueryClient();
  const { data, isFetching, refetch } = useSuspenseQuery(schedulesQuery());
  const jobs = data.jobs;
  const backend = data.backend;
  const [modalOpen, setModalOpen] = useState(false);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: queryKeys.schedules });

  const add = useMutation({
    mutationFn: (body: { prompt: string; schedule: string; job_id?: string }) => api.addSchedule(body),
    onSuccess: () => setModalOpen(false),
    onSettled: invalidate,
  });
  const cancel = useMutation({ mutationFn: (id: string) => api.cancelSchedule(id), onSettled: invalidate });
  const busy = add.isPending || cancel.isPending;

  return (
    <>
      <PanelHeader
        title="Schedule"
        kicker={`${jobs.length} job${jobs.length === 1 ? "" : "s"} · ${backend}`}
        actions={
          <>
            <button className="icon-button" type="button" onClick={() => void refetch()} disabled={isFetching} title="Refresh">
              <RefreshCw size={16} className={isFetching ? "spin" : ""} />
            </button>
            <button className="primary-button" type="button" onClick={() => setModalOpen(true)}
                    disabled={backend === "disabled"} data-testid="schedule-new">
              <Plus size={16} /> New schedule
            </button>
          </>
        }
      />

      <div className="stage-body">
        {add.isError ? <p className="settings-status">Couldn't schedule: {add.error instanceof Error ? add.error.message : String(add.error)}</p> : null}
        <div className="subagent-list">
          {jobs.length ? (
            jobs.map((job) => (
              <div className="subagent-row" key={job.id}>
                <div>
                  <strong>{job.id}</strong>
                  <span>
                    {describeSchedule(job.schedule)}
                    {job.next_fire ? ` · next ${job.next_fire}` : ""}
                    {" · "}
                    {job.prompt.length > 80 ? `${job.prompt.slice(0, 80)}…` : job.prompt}
                  </span>
                </div>
                <button className="icon-button" type="button" onClick={() => cancel.mutate(job.id)}
                        disabled={busy} title="Cancel job">
                  <Trash2 size={16} />
                </button>
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
    </>
  );
}

export function SchedulePanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="schedule" />}>
            <Suspense fallback={<PanelSkeleton label="Loading schedule…" />}>
              <ScheduleBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
