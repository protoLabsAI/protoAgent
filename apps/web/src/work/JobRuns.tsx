/**
 * Work view ▸ a scheduled job's Recent runs (work-view-additions §2.3–§2.4, #5543).
 *
 * Mounted in the detail of a Time follow-up only, below the actions and above Technical
 * details. It reads `GET /api/scheduler/jobs/{id}/runs?limit=20`, newest first, and draws
 * one bordered `ul` labelled by its `h3` — one `li` per run, never a button (there is
 * nothing to open). Every time is in the job's own clock with the zone name in the text.
 * The list has no live region: it changes only on open, on Show older runs, or when the
 * job itself records another fire.
 */
import { useEffect, useId, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { Skeleton, StatusDot } from "@protolabsai/ui/data";
import { Accordion, AccordionItem } from "@protolabsai/ui/navigation";
import { Button, Callout, type Status } from "@protolabsai/ui/primitives";

import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import type { ScheduledJob, ScheduledJobRun } from "../lib/types";
import { useWorkActionSize } from "../shell/SurfaceFrame";
import { jobClockTime, jobZone } from "./rowMeta";
import "./job-runs.css";

/** The first page, and the one larger page Show older runs asks for — the route's maximum
 *  (the run history has no cursor, spec gap S3). */
export const RUNS_PAGE = 20;
export const RUNS_MAX = 200;

export type RunPresentation = { word: string; tone: Status; note: string };

/** The §2.3 table, matched top to bottom — the canceled row is checked before the generic
 *  `ok: null` row, in both spellings the scheduler matches. Failures use warning, never error. */
export function runPresentation(run: ScheduledJobRun): RunPresentation {
  if (run.state === "settled" && run.ok === true) return { word: "Ran", tone: "success", note: "Completed" };
  if (run.state === "settled" && run.ok === false) return { word: "Failed", tone: "warning", note: (run.error ?? "").trim() };
  switch (run.state) {
    case "running":
      return { word: "Running", tone: "info", note: "Started, still going." };
    case "claimed":
      return { word: "Not confirmed", tone: "neutral", note: "It was due, but there's no record it started." };
    case "unknown":
      return { word: "Not confirmed", tone: "neutral", note: "It started, but the result never came back. It may still finish." };
    case "retryable":
      return { word: "Not run", tone: "neutral", note: "It didn't start. This slot is still owed, so it will try again." };
    case "interrupted":
      return { word: "Skipped", tone: "neutral", note: "A restart interrupted it." };
    case "interrupted_needs_operator":
      return { word: "Waiting on you", tone: "neutral", note: "A restart interrupted it. It's in Needs you." };
  }
  if (run.state === "settled" && (run.task_state === "canceled" || run.task_state === "cancelled")) {
    return { word: "Canceled", tone: "neutral", note: "It was canceled before it finished." };
  }
  if (run.state === "settled") return { word: "Ran", tone: "neutral", note: "Outcome not recorded." };
  // A dispatch state this console doesn't know yet: claim nothing about how it went.
  return { word: "Not confirmed", tone: "neutral", note: "Outcome not recorded." };
}

/** "42 s", "3 min 5 s", "1 h 12 min" — "" when the run carries no usable duration. */
export function runDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 1) return "under 1 s";
  const total = Math.round(seconds);
  if (total < 60) return `${total} s`;
  if (total < 3600) {
    const rest = total % 60;
    return rest ? `${Math.floor(total / 60)} min ${rest} s` : `${Math.floor(total / 60)} min`;
  }
  const minutes = Math.round((total % 3600) / 60);
  const hours = Math.floor(total / 3600) + (minutes === 60 ? 1 : 0);
  return minutes && minutes !== 60 ? `${hours} h ${minutes} min` : `${hours} h`;
}

/** A run from an older generation of the id: its `job_created_at` isn't the job's `created_at`. */
function fromEarlierVersion(run: ScheduledJobRun, job: ScheduledJob): boolean {
  if (!run.job_created_at || !job.created_at) return false;
  if (run.job_created_at === job.created_at) return false;
  const a = new Date(run.job_created_at).getTime();
  const b = new Date(job.created_at).getTime();
  return !(Number.isFinite(a) && Number.isFinite(b) && a === b);
}

const TECHNICAL_KEYS = ["state", "task_state", "scheduled_for", "task_id", "message_id", "job_created_at"] as const;

function RunRow({ run, job, zone }: { run: ScheduledJobRun; job: ScheduledJob; zone: string }) {
  const { word, tone, note } = runPresentation(run);
  const when = jobClockTime(run.scheduled_for, zone);
  const took = runDuration(run.duration_s);
  // Line 2: the outcome, then what S2 adds when the ledger recorded it — a completed run
  // that posted nothing "stayed silent" — then the older-generation note.
  const notes = [note];
  if (run.reported === false) notes.push("stayed silent");
  if (fromEarlierVersion(run, job)) notes.push("From an earlier version of this job");
  const line = notes.filter(Boolean).join(" · ");
  const technical = TECHNICAL_KEYS
    .map((key) => [key, run[key]] as const)
    .filter(([, value]) => value != null && value !== "");
  return (
    <li className="job-run">
      <div className="job-run__line">
        <StatusDot status={tone} />
        <span className="job-run__word">{word}</span>
        {when && <span className="pl-chip-meta">{when}</span>}
        {took && <span className="pl-chip-meta">{took}</span>}
      </div>
      {line && <p className="job-run__note">{line}</p>}
      {technical.length > 0 && (
        <Accordion>
          <AccordionItem title="Technical details">
            <dl className="work-detail__tech">
              {technical.map(([key, value]) => (
                <div className="work-detail__tech-row" key={key}>
                  <dt>{key}</dt>
                  <dd className="work-detail__tech-value">{String(value)}</dd>
                </div>
              ))}
            </dl>
          </AccordionItem>
        </Accordion>
      )}
    </li>
  );
}

type RunsPage = { runs: ScheduledJobRun[]; limit: number };

export function JobRuns({ job }: { job: ScheduledJob }) {
  const size = useWorkActionSize();
  const headingId = useId();
  const zone = jobZone(job);
  // The page size the NEXT fetch asks for: 20 on open, 200 once Show older runs is pressed.
  const limit = useRef(RUNS_PAGE);
  const query = useQuery({
    queryKey: queryKeys.scheduleRuns(job.id),
    queryFn: async (): Promise<RunsPage> => {
      const asked = limit.current;
      const res = await api.scheduleRuns(job.id, asked);
      return { runs: res.runs ?? [], limit: asked };
    },
    // Every open starts from the first page again, and a failure is the operator's call to
    // Retry — a 404 or 501 would never succeed on a silent second try anyway.
    gcTime: 0,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const { refetch } = query;

  // The job recorded another fire while its detail stayed open: bring the list up to date.
  const fired = useRef(job.last_fire);
  useEffect(() => {
    if (fired.current === job.last_fire) return;
    fired.current = job.last_fire;
    void refetch();
  }, [job.last_fire, refetch]);

  function showOlder() {
    limit.current = RUNS_MAX;
    void refetch();
  }

  const page = query.data;
  const status = (query.error as { status?: number } | null)?.status;
  const retry = <Button size={size} disabled={query.isFetching} onClick={() => void refetch()}>Retry</Button>;

  let body;
  if (page && page.runs.length > 0) {
    body = (
      <>
        {query.isError && (
          <Callout tone="warning">
            Showing the last load. Couldn&apos;t refresh. {retry}
          </Callout>
        )}
        <ul className="work-group job-runs__list" aria-labelledby={headingId}>
          {page.runs.map((run, i) => (
            <RunRow key={`${run.scheduled_for}:${run.job_created_at ?? ""}:${i}`} run={run} job={job} zone={zone} />
          ))}
        </ul>
        {/* Only when the page came back full — and never again once the larger page was
            asked for, even if that fetch failed (Retry above re-asks for it). */}
        {page.limit < RUNS_MAX && limit.current < RUNS_MAX && page.runs.length >= page.limit && (
          <div>
            <Button variant="ghost" size={size} disabled={query.isFetching} onClick={showOlder}>Show older runs</Button>
          </div>
        )}
        {page.limit >= RUNS_MAX && page.runs.length >= RUNS_MAX && (
          <p className="job-runs__note">Showing the latest {RUNS_MAX} runs.</p>
        )}
      </>
    );
  } else if (query.isError) {
    body = status === 404
      ? <p className="job-runs__note">This job is gone and left no runs.</p>
      : status === 501
        ? <p className="job-runs__note">Run history isn&apos;t available. This scheduler backend doesn&apos;t keep run history.</p>
        : (
          <Callout tone="warning">
            Couldn&apos;t load the run history. The job itself is fine. Only this list failed to load. {retry}
          </Callout>
        );
  } else if (page) {
    const first = job.next_fire ? jobClockTime(job.next_fire, zone, Date.now(), false) : "";
    body = (
      <p className="job-runs__note">
        {job.enabled === false
          ? "No runs yet. It's turned off, so it won't run until you turn it on."
          : first ? `No runs yet. It first runs ${first}.` : "No runs yet."}
      </p>
    );
  } else {
    body = <div aria-busy="true" aria-label="Loading recent runs"><Skeleton lines={2} /></div>;
  }

  return (
    <section className="job-runs" aria-labelledby={headingId}>
      <h3 id={headingId} className="job-runs__heading">Recent runs</h3>
      {body}
    </section>
  );
}
