import { useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { StatusDot } from "@protolabsai/ui/data";
import { Button, type Status } from "@protolabsai/ui/primitives";

import type { ConversationRow } from "../lib/api/chat";
import { tasksQuery } from "../lib/queries";
import type { Task } from "../lib/types";
import { useUI } from "../state/uiStore";
import { fromTask, groupFor, type WorkGroupId } from "../work/tone";

// ThreadJobs — the reader's collapsed "Jobs from this thread" line (docs/design/
// other-conversations.md §11.1 item 1, ADR 0131 P2c; build slice C10a). It renders the summary
// button and, when expanded, one row per open job bound to the thread. "Jobs" are tasks: the API,
// store and code say task; Work's UI says job, so every user-facing string here says "job"
// (§11 intro). This component is not mounted anywhere yet — C10b places it in the reader header.

// Status → word + dot tone + sort rank (§11.3 "Status words"). Dot tones follow the From others
// list's convention (ConversationsPane): warning = needs you, info = running; `closed` is never
// shown. `blocked` takes `error` so a stuck job reads distinctly from a `waiting` (warning) one.
const STATUS_META: Record<string, { word: string; tone: Status; rank: number }> = {
  waiting: { word: "waiting on you", tone: "warning", rank: 0 },
  in_progress: { word: "in progress", tone: "info", rank: 1 },
  blocked: { word: "blocked", tone: "error", rank: 2 },
  open: { word: "not started", tone: "neutral", rank: 3 },
  deferred: { word: "not started", tone: "neutral", rank: 3 },
};
// Any non-closed status we don't specifically recognise reads as "not started" (still shown:
// §11, "'Open' means any status except closed").
const NOT_STARTED = { word: "not started", tone: "neutral" as Status, rank: 3 };

type JobView = { id: string; title: string; word: string; tone: Status; rank: number; waiting: boolean; group: WorkGroupId | null };

/** Resolve the row's `tasks` snapshot into the shown jobs. The snapshot can be stale —
 *  `conversations-store` refetches only on a2a turn events — so when the Work tasks query has a
 *  job loaded, ITS status and title win (no new fetch is added). A job the query reports `closed`
 *  is dropped; so is a snapshot `closed`. Ordered waiting → in progress → blocked → not started
 *  (a stable sort keeps the row's order within a rank). */
export function deriveThreadJobs(row: ConversationRow, byId: Map<string, Task>): JobView[] {
  const jobs: JobView[] = [];
  for (const snap of row.tasks ?? []) {
    const live = byId.get(snap.id);
    const status = (live?.status ?? snap.status) || "";
    if (status === "closed") continue; // never shown or counted
    const title = live?.title ?? snap.title;
    const meta = STATUS_META[status] ?? NOT_STARTED;
    const group = groupFor({ kind: "task", id: snap.id, title, meta: "", status: fromTask(status || "open"), raw: { ...snap, status } });
    jobs.push({ id: snap.id, title, word: meta.word, tone: meta.tone, rank: meta.rank, waiting: status === "waiting", group });
  }
  // Array.prototype.sort is stable (ES2019+), so equal-rank jobs keep their snapshot order.
  jobs.sort((a, b) => a.rank - b.rank);
  return jobs;
}

export function ThreadJobs({ row }: { row: ConversationRow }) {
  // Collapsed on every mount — open/closed is deliberately not remembered between threads
  // (§11.1 item 1: "each thread opens collapsed").
  const [expanded, setExpanded] = useState(false);
  const listId = useId();

  // Read the Work tasks cache WITHOUT adding a fetch (`enabled: false`): if the Work panel has
  // loaded it, this observer reflects its rows and re-renders on its updates; otherwise `data`
  // is undefined and the row snapshot is used as-is.
  const { data } = useQuery({ ...tasksQuery(), enabled: false });
  const byId = useMemo(() => {
    const m = new Map<string, Task>();
    for (const t of data?.issues ?? []) m.set(t.id, t);
    return m;
  }, [data]);

  const jobs = useMemo(() => deriveThreadJobs(row, byId), [row, byId]);

  // No open jobs → the line is not drawn (§11.2); the header is exactly as in revision 4.
  if (jobs.length === 0) return null;

  const count = jobs.length;
  const needsYou = jobs.filter((j) => j.waiting).length;
  // The accessible name spells the status in words with commas; the visible label uses middots.
  const accessibleName = `Jobs from this thread, ${count}${needsYou ? `, ${needsYou} needs you` : ""}`;

  return (
    <div className="thread-jobs">
      <Button
        variant="ghost"
        size="sm"
        type="button"
        className="thread-jobs__summary"
        aria-expanded={expanded}
        aria-controls={listId}
        aria-label={accessibleName}
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="thread-jobs__summary-text">
          Jobs from this thread · {count}
          {needsYou > 0 ? (
            <span className="thread-jobs__needs">
              {" · "}
              {/* The "needs you" marker — a warning dot, aria-hidden (the count already reads it). */}
              <StatusDot status="warning" />
              {`${needsYou} needs you`}
            </span>
          ) : null}
        </span>
      </Button>
      {expanded ? (
        <ul id={listId} className="thread-jobs__list">
          {jobs.map((job) => (
            <li key={job.id} className="thread-jobs__item">
              {/* A composite row button (protoContent#551: a raw <button> for a multi-element row,
                  carrying the DS focus ring + tokens). It opens the job's Work detail. */}
              <button
                type="button"
                className="thread-jobs__row"
                aria-label={`Open job: ${job.title}, ${job.word}`}
                onClick={() =>
                  useUI.getState().openSurfaceModal("work", {
                    section: job.group ?? "tasks",
                    itemId: `task:${job.id}`,
                    filters: { thisChat: false },
                  })
                }
              >
                <StatusDot status={job.tone} />
                <span className="thread-jobs__row-title">{job.title}</span>
                <span className="thread-jobs__row-status">{job.word}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
