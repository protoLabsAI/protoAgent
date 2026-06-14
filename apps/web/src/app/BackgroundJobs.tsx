import { Dialog } from "@protolabsai/ui/overlays";
import { Bot, CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { api } from "../lib/api";
import { onTopic } from "../lib/events";
import type { BackgroundJobDTO } from "../lib/types";
import { byRecency, fmtElapsed, nowIso } from "./background-jobs";

// Background-jobs UtilityBar pill + dialog (ADR 0050 Phase 3). Hydrates from
// GET /api/background, then tracks live via the `background.{started,completed}`
// bus events (scoped to this window's agent). The pill shows a spinner + count
// while jobs run and an unread dot when jobs finish; clicking opens a dialog
// listing each job's status, elapsed time, and (for finished jobs) its result.
// Read-only — stop/kill controls are Phase 4. (Pure helpers live in
// ./background-jobs so they're unit-testable without a react-dom import.)

export function BackgroundJobs() {
  const [enabled, setEnabled] = useState(false);
  const [jobs, setJobs] = useState<Record<string, BackgroundJobDTO>>({});
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  const [, setTick] = useState(0); // re-render for live elapsed while open

  // Hydrate from the durable registry.
  useEffect(() => {
    let alive = true;
    api
      .background()
      .then((d) => {
        if (!alive) return;
        setEnabled(!!d.enabled);
        const m: Record<string, BackgroundJobDTO> = {};
        for (const j of d.jobs || []) m[j.id] = j;
        setJobs(m);
      })
      .catch(() => {
        /* feature off / unreachable — the pill stays hidden */
      });
    return () => {
      alive = false;
    };
  }, []);

  // Live updates off the event bus.
  useEffect(() => {
    const upsert = (id: string, patch: Partial<BackgroundJobDTO>) =>
      setJobs((m) => {
        const prev = m[id];
        const next: BackgroundJobDTO = {
          id,
          status: patch.status ?? prev?.status ?? "running",
          subagent_type: patch.subagent_type ?? prev?.subagent_type ?? "",
          description: patch.description ?? prev?.description ?? "",
          origin_session: patch.origin_session ?? prev?.origin_session,
          result: patch.result ?? prev?.result,
          created_at: patch.created_at ?? prev?.created_at,
          completed_at: patch.completed_at ?? prev?.completed_at,
        };
        return { ...m, [id]: next };
      });

    const offStart = onTopic("background.started", (d) => {
      const id = String(d.job_id || "");
      if (!id) return;
      setEnabled(true);
      upsert(id, {
        status: "running",
        subagent_type: String(d.subagent_type || ""),
        description: String(d.description || ""),
        origin_session: String(d.origin_session || ""),
        created_at: nowIso(),
      });
    });

    const offDone = onTopic("background.completed", (d) => {
      const id = String(d.job_id || "");
      if (!id) return;
      upsert(id, {
        status: String(d.status) === "failed" ? "failed" : "completed",
        subagent_type: String(d.subagent_type || ""),
        description: String(d.description || ""),
        origin_session: String(d.origin_session || ""),
        result: String(d.result || ""),
        completed_at: nowIso(),
      });
      setUnread((u) => u + 1);
    });

    return () => {
      offStart();
      offDone();
    };
  }, []);

  const list = useMemo(() => Object.values(jobs).sort(byRecency), [jobs]);
  const running = list.filter((j) => j.status === "running").length;

  // Tick the elapsed clock once a second while the dialog is open and work runs.
  useEffect(() => {
    if (!open || running === 0) return;
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [open, running]);

  if (!enabled && list.length === 0) return null;

  return (
    <>
      <button
        type="button"
        className="util-btn bg-jobs-pill"
        onClick={() => {
          setOpen(true);
          setUnread(0);
        }}
        title="Background agents"
        aria-label={`Background agents${running ? ` — ${running} running` : ""}`}
        data-testid="background-jobs-pill"
      >
        {running > 0 ? <Loader2 size={13} className="spin" /> : <Bot size={13} />}
        {running > 0 ? <span>{running}</span> : null}
        {unread > 0 ? <span className="bg-jobs-unread" aria-label={`${unread} finished`} /> : null}
      </button>
      {open ? (
        <Dialog open onClose={() => setOpen(false)} title="Background agents" width="min(640px, 94vw)">
          {list.length === 0 ? (
            <p className="bg-jobs-empty">No background agents have run yet.</p>
          ) : (
            <ul className="bg-jobs-list">
              {list.map((j) => (
                <BgJobRow key={j.id} job={j} />
              ))}
            </ul>
          )}
        </Dialog>
      ) : null}
    </>
  );
}

function BgJobRow({ job }: { job: BackgroundJobDTO }) {
  const [expanded, setExpanded] = useState(false);
  const icon =
    job.status === "running" ? (
      <Loader2 size={14} className="spin" />
    ) : job.status === "failed" ? (
      <XCircle size={14} className="bg-jobs-fail" />
    ) : (
      <CheckCircle2 size={14} className="bg-jobs-ok" />
    );
  const elapsed = fmtElapsed(job.created_at, job.status === "running" ? undefined : job.completed_at);
  const hasResult = job.status !== "running" && !!job.result;
  return (
    <li className="bg-jobs-row">
      <button
        type="button"
        className="bg-jobs-rowhead"
        onClick={() => hasResult && setExpanded((v) => !v)}
        aria-expanded={hasResult ? expanded : undefined}
        disabled={!hasResult}
      >
        <span className="bg-jobs-icon">{icon}</span>
        <span className="bg-jobs-meta">
          <span className="bg-jobs-title">
            <strong>{job.subagent_type || "agent"}</strong> — {job.description || "(no description)"}
          </span>
          <span className="bg-jobs-sub">
            {job.status}
            {elapsed ? ` · ${elapsed}` : ""}
            {hasResult ? (expanded ? " · hide result" : " · show result") : ""}
          </span>
        </span>
      </button>
      {hasResult && expanded ? (
        <div className="bg-jobs-result">
          <Markdown>{job.result || ""}</Markdown>
        </div>
      ) : null}
    </li>
  );
}
