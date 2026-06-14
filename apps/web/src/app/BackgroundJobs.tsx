import { Dialog } from "@protolabsai/ui/overlays";
import { Bot, CheckCircle2, Loader2, Square, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { api } from "../lib/api";
import { onTopic } from "../lib/events";
import type { BackgroundJobDTO } from "../lib/types";
import { applyProgress, byRecency, fmtElapsed, nowIso, type ProgressTool } from "./background-jobs";

// Background-jobs UtilityBar pill + dialog (ADR 0050 Phase 3 / ADR 0051). Hydrates from
// GET /api/background, then tracks live via the bus: `background.{started,completed}` for
// lifecycle and `background.progress` for a running job's tool-by-tool feed. The pill shows
// a spinner + count while jobs run and an unread dot when jobs finish; the dialog lists each
// job's status, elapsed, live tool activity, result (markdown), and a Stop control for
// running jobs. (Pure helpers live in ./background-jobs — react-dom-free + unit-tested.)

export function BackgroundJobs() {
  const [enabled, setEnabled] = useState(false);
  const [jobs, setJobs] = useState<Record<string, BackgroundJobDTO>>({});
  const [progress, setProgress] = useState<Record<string, ProgressTool[]>>({});
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

    const offProgress = onTopic("background.progress", (d) => {
      const id = String(d.job_id || "");
      if (!id) return;
      setProgress((p) => ({
        ...p,
        [id]: applyProgress(p[id] || [], {
          phase: String(d.phase || ""),
          tool: d.tool ? String(d.tool) : undefined,
          tool_call_id: d.tool_call_id ? String(d.tool_call_id) : undefined,
          error: !!d.error,
        }),
      }));
    });

    const offDone = onTopic("background.completed", (d) => {
      const id = String(d.job_id || "");
      if (!id) return;
      const status = String(d.status);
      upsert(id, {
        status: status === "failed" || status === "canceled" ? (status as BackgroundJobDTO["status"]) : "completed",
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
      offProgress();
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

  async function stop(jobId: string) {
    // Optimistic — flip to canceled immediately; the bus completion confirms.
    setJobs((m) => (m[jobId] ? { ...m, [jobId]: { ...m[jobId], status: "canceled" } } : m));
    try {
      await api.stopBackground(jobId);
    } catch {
      /* best-effort; the registry stays source of truth */
    }
  }

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
                <BgJobRow key={j.id} job={j} tools={progress[j.id] || []} onStop={stop} />
              ))}
            </ul>
          )}
        </Dialog>
      ) : null}
    </>
  );
}

function BgJobRow({
  job,
  tools,
  onStop,
}: {
  job: BackgroundJobDTO;
  tools: ProgressTool[];
  onStop: (jobId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const running = job.status === "running";
  const icon = running ? (
    <Loader2 size={14} className="spin" />
  ) : job.status === "failed" ? (
    <XCircle size={14} className="bg-jobs-fail" />
  ) : (
    <CheckCircle2 size={14} className="bg-jobs-ok" />
  );
  const elapsed = fmtElapsed(job.created_at, running ? undefined : job.completed_at);
  const hasResult = !running && !!job.result;
  const recentTools = tools.slice(-3);
  return (
    <li className="bg-jobs-row">
      <div className="bg-jobs-rowhead">
        <button
          type="button"
          className="bg-jobs-rowmain"
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
            {running && recentTools.length > 0 ? (
              <span className="bg-jobs-tools">
                {recentTools.map((t) => (
                  <span key={t.id} className={`bg-jobs-tool ${t.error ? "is-err" : t.done ? "is-done" : "is-run"}`}>
                    {t.error ? "✗" : t.done ? "✓" : "⊷"} {t.tool}
                  </span>
                ))}
              </span>
            ) : null}
          </span>
        </button>
        {running ? (
          <button
            type="button"
            className="bg-jobs-stop"
            onClick={() => onStop(job.id)}
            title="Stop this background agent"
            aria-label="Stop"
          >
            <Square size={12} />
          </button>
        ) : null}
      </div>
      {hasResult && expanded ? (
        <div className="bg-jobs-result">
          <Markdown>{job.result || ""}</Markdown>
        </div>
      ) : null}
    </li>
  );
}
