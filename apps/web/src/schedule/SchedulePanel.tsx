import {
  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { Suspense, useState } from "react";

import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { api } from "../lib/api";
import { queryKeys, schedulesQuery } from "../lib/queries";

// Scheduled jobs (Activity → Schedule), on the TanStack Query data layer
// (ADR 0013): the job list is a useSuspenseQuery; add/cancel are useMutations
// that invalidate it. Loading via <Suspense>, errors via <ErrorBoundary>.

function ScheduleBody() {
  const queryClient = useQueryClient();
  const { data, isFetching, refetch } = useSuspenseQuery(schedulesQuery());
  const jobs = data.jobs;
  const backend = data.backend;

  const [prompt, setPrompt] = useState("");
  const [when, setWhen] = useState("");
  const [jobId, setJobId] = useState("");

  const invalidate = () => queryClient.invalidateQueries({ queryKey: queryKeys.schedules });

  const add = useMutation({
    mutationFn: () =>
      api.addSchedule({ prompt: prompt.trim(), schedule: when.trim(), job_id: jobId.trim() || undefined }),
    onSuccess: () => {
      setPrompt("");
      setWhen("");
      setJobId("");
    },
    onSettled: invalidate,
  });
  const cancel = useMutation({ mutationFn: (id: string) => api.cancelSchedule(id), onSettled: invalidate });

  const busy = add.isPending || cancel.isPending;

  return (
    <>
      <div className="panel-header">
        <div>
          <h1>Schedule</h1>
          <p className="panel-kicker">{jobs.length} job{jobs.length === 1 ? "" : "s"} · {backend}</p>
        </div>
        <button className="icon-button" type="button" onClick={() => void refetch()} disabled={isFetching} title="Refresh">
          <RefreshCw size={16} className={isFetching ? "spin" : ""} />
        </button>
      </div>

      <div className="stage-body">
        <div className="subagent-grid">
          <label className="field">
            <span>When (cron or ISO datetime)</span>
            <input
              value={when}
              onChange={(event) => setWhen(event.target.value)}
              placeholder='e.g. "0 9 * * 1-5"  or  "2026-06-01T15:00:00Z"'
            />
          </label>
          <label className="field">
            <span>Job id (optional)</span>
            <input value={jobId} onChange={(event) => setJobId(event.target.value)} placeholder="auto" />
          </label>
          <button
            className="primary-button"
            type="button"
            onClick={() => add.mutate()}
            disabled={busy || !prompt.trim() || !when.trim()}
          >
            <Plus size={16} />
            Schedule
          </button>
        </div>
        <label className="field grow">
          <span>Prompt (delivered to the agent when it fires)</span>
          <textarea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="What the agent should do when this fires"
            rows={5}
          />
        </label>

        <div className="subagent-list">
          {jobs.length ? (
            jobs.map((job) => (
              <div className="subagent-row" key={job.id}>
                <div>
                  <strong>{job.id}</strong>
                  <span>
                    {job.schedule}
                    {job.next_fire ? ` · next ${job.next_fire}` : ""}
                    {" · "}
                    {job.prompt.length > 80 ? `${job.prompt.slice(0, 80)}…` : job.prompt}
                  </span>
                </div>
                <button
                  className="icon-button"
                  type="button"
                  onClick={() => cancel.mutate(job.id)}
                  disabled={busy}
                  title="Cancel job"
                >
                  <Trash2 size={16} />
                </button>
              </div>
            ))
          ) : (
            <div className="subagent-row">
              <div>
                <strong>No scheduled jobs</strong>
                <span>{backend !== "local" && backend !== "disabled" ? `jobs may be managed remotely by ${backend}` : "create one above"}</span>
              </div>
            </div>
          )}
        </div>
      </div>
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
