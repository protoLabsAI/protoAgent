import { useEffect, useMemo, useSyncExternalStore } from "react";

import { api } from "../lib/api";
import { onConnectionChange, onTopic } from "../lib/events";
import type { BackgroundJobDTO } from "../lib/types";

// Background jobs as the CHAT needs them: which are still running, per session, and the
// status of one job by id. The delegation row tracks its background job here, and the
// "background work in this chat" strip + tab dot read which of a session's jobs are live.
//
// The utility-bar widget (BackgroundJobs.tsx) keeps its own richer copy — progress, unread,
// full results — for its dialog; this store is deliberately just status, hydrated from the
// same `GET /api/background` and kept live by the same `background.{started,completed}` bus
// events, so the two can't disagree for long.

export type JobStatus = BackgroundJobDTO["status"];
export type JobLite = Pick<BackgroundJobDTO, "id" | "status" | "subagent_type" | "description" | "origin_session">;

let jobs: Record<string, JobLite> = {};
const listeners = new Set<() => void>();
const fetched = new Set<string>(); // single-job lookups already requested (no refetch loop)
let started = false;
let stop: (() => void) | null = null;

function emit() {
  for (const l of listeners) l();
}

function upsert(id: string, patch: Partial<JobLite>) {
  const prev = jobs[id];
  jobs = {
    ...jobs,
    [id]: {
      id,
      status: patch.status ?? prev?.status ?? "running",
      subagent_type: patch.subagent_type ?? prev?.subagent_type ?? "",
      description: patch.description ?? prev?.description ?? "",
      origin_session: patch.origin_session ?? prev?.origin_session,
    },
  };
  emit();
}

function statusOf(raw: unknown): JobStatus {
  const s = String(raw ?? "");
  return s === "failed" || s === "canceled" || s === "running" ? s : "completed";
}

function hydrate() {
  api
    .background()
    .then((d) => {
      const next = { ...jobs };
      for (const j of d.jobs || []) {
        next[j.id] = {
          id: j.id,
          status: j.status,
          subagent_type: j.subagent_type,
          description: j.description,
          origin_session: j.origin_session,
        };
      }
      jobs = next;
      emit();
    })
    .catch(() => {
      /* background jobs off / unreachable — nothing is running, as far as the chat knows */
    });
}

function start() {
  if (started) return;
  started = true;
  // Hydrate now (a token-gated setup whose bus can't authenticate still reads plain HTTP)
  // and on every later reconnect — `onConnectionChange` also reports the CURRENT state as
  // soon as it's subscribed, which the flag skips so startup is one request, not two.
  hydrate();
  let initial = true;
  const offConn = onConnectionChange((c) => {
    if (initial) {
      initial = false;
      return;
    }
    if (c) hydrate();
  });
  const offStart = onTopic("background.started", (d) => {
    const id = String(d.job_id || "");
    if (!id) return;
    upsert(id, {
      status: "running",
      subagent_type: String(d.subagent_type || ""),
      description: String(d.description || ""),
      origin_session: String(d.origin_session || "") || undefined,
    });
  });
  const offDone = onTopic("background.completed", (d) => {
    const id = String(d.job_id || "");
    if (!id) return;
    upsert(id, {
      status: statusOf(d.status),
      subagent_type: String(d.subagent_type || ""),
      description: String(d.description || ""),
      origin_session: String(d.origin_session || "") || undefined,
    });
  });
  stop = () => {
    offConn();
    offStart();
    offDone();
  };
}

function subscribe(l: () => void) {
  start();
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

const snapshot = () => jobs;

/** Resolve a job the list doesn't have yet — a delegation row reloaded from history. */
function ensureJob(id: string) {
  if (jobs[id] || fetched.has(id)) return;
  fetched.add(id);
  api
    .backgroundJob(id)
    .then((j) =>
      upsert(j.id, {
        status: j.status,
        subagent_type: j.subagent_type,
        description: j.description,
        origin_session: j.origin_session,
      }),
    )
    .catch(() => {
      /* pruned / unavailable — the row just shows no live status */
    });
}

/** One background job's live status (undefined until known, or with no id). */
export function useBackgroundJob(id?: string): JobLite | undefined {
  const all = useSyncExternalStore(subscribe, snapshot, snapshot);
  useEffect(() => {
    if (id) ensureJob(id);
  }, [id]);
  return id ? all[id] : undefined;
}

/** The session's background jobs that are still running, oldest first. */
export function runningJobsFor(all: Record<string, JobLite>, sessionId: string): JobLite[] {
  return Object.values(all).filter((j) => j.status === "running" && j.origin_session === sessionId);
}

export function useRunningBackgroundJobs(sessionId: string): JobLite[] {
  const all = useSyncExternalStore(subscribe, snapshot, snapshot);
  return useMemo(() => runningJobsFor(all, sessionId), [all, sessionId]);
}

/** Every session with background work still running — for the tab strip's dot. */
export function useSessionsWithBackgroundWork(): Set<string> {
  const all = useSyncExternalStore(subscribe, snapshot, snapshot);
  return useMemo(() => {
    const out = new Set<string>();
    for (const j of Object.values(all)) if (j.status === "running" && j.origin_session) out.add(j.origin_session);
    return out;
  }, [all]);
}

/** Test seam: seed / reset the store without the bus or the API. */
export function __setJobsForTest(next: Record<string, JobLite>) {
  jobs = next;
  started = true; // keep subscribe() from wiring the real bus/API in unit tests
  emit();
}

export function __resetForTest() {
  stop?.();
  stop = null;
  jobs = {};
  fetched.clear();
  started = false;
  emit();
}
