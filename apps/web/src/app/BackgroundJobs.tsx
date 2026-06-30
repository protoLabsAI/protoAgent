import { Dialog, Tooltip } from "@protolabsai/ui/overlays";
import { ToolCard, ToolCardList, ToolSection } from "@protolabsai/ui/tool-card";
import { Spinner } from "@protolabsai/ui/data";
import { Bot, CheckCircle2, Square, Trash2, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { api } from "../lib/api";
import { onConnectionChange, onTopic } from "../lib/events";
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

  // Pull the durable registry — every job's FULL result. The live bus only carries a
  // trimmed ~2k preview (so a still-open chat can render the outcome without a refetch),
  // so the API is the source of truth for the dialog. We re-hydrate when the dialog opens
  // and when a job completes, so "show result" always renders the entire report.
  const hydrate = useCallback(() => {
    api
      .background()
      .then((d) => {
        setEnabled(!!d.enabled);
        const m: Record<string, BackgroundJobDTO> = {};
        for (const j of d.jobs || []) m[j.id] = j;
        setJobs(m);
      })
      .catch(() => {
        /* feature off / unreachable — the pill stays hidden */
      });
  }, []);

  // Hydrate on mount, then again whenever the event bus (re)connects. The reconnect
  // refetch is what makes the pill appear WITHOUT a manual reload when the backend wasn't
  // reachable at first paint: the shell (and this widget) mounts immediately while a cold
  // sidecar is still warming up — the BootGate splash only overlays it — so the one-shot
  // mount fetch can fail (connection refused) before the engine is up, and nothing else
  // re-fetches the *existence* of the feature. `onConnectionChange` fires the current state
  // immediately and on every transition; we refetch on each connect (also refreshing after
  // a server restart). The plain mount call covers token-gated setups where the SSE bus
  // can't authenticate but plain HTTP can.
  useEffect(() => {
    hydrate();
    return onConnectionChange((c) => {
      if (c) hydrate();
    });
  }, [hydrate]);

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
          output: d.output ? String(d.output) : undefined,
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
      // The event's `result` is the trimmed preview — pull the durable full result so
      // "show result" renders the entire report, not the …[truncated] copy.
      hydrate();
    });

    return () => {
      offStart();
      offProgress();
      offDone();
    };
  }, [hydrate]);

  const list = useMemo(() => Object.values(jobs).sort(byRecency), [jobs]);
  const running = list.filter((j) => j.status === "running").length;
  const finished = list.length - running;

  // Hover popover copy (matches the Inbox/Activity widgets), reflecting live state.
  const info =
    running > 0
      ? `${running} background agent${running === 1 ? "" : "s"} running`
      : unread > 0
        ? `${unread} finished — click to review`
        : "Background agents — work running on its own";

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

  // Delete a single finished entry — optimistic remove; the registry is source of truth.
  async function del(jobId: string) {
    setJobs((m) => {
      const next = { ...m };
      delete next[jobId];
      return next;
    });
    try {
      await api.deleteBackground(jobId);
    } catch {
      /* best-effort */
    }
  }

  // Clear all finished entries at once (running jobs stay).
  async function clearFinished() {
    setJobs((m) => Object.fromEntries(Object.entries(m).filter(([, j]) => j.status === "running")));
    try {
      await api.clearFinishedBackground();
    } catch {
      /* best-effort */
    }
  }

  if (!enabled && list.length === 0) return null;

  return (
    <>
      <Tooltip label={info}>
        <button
          type="button"
          className="util-btn bg-jobs-pill"
          onClick={() => {
            setOpen(true);
            setUnread(0);
            hydrate(); // fetch the FULL results when the panel opens (replaces any live previews)
          }}
          aria-label={`Background agents${running ? ` — ${running} running` : ""}`}
          data-testid="background-jobs-pill"
        >
          {running > 0 ? <Spinner size={13} /> : <Bot size={13} />}
          {running > 0 ? <span>{running}</span> : null}
          {unread > 0 ? <span className="bg-jobs-unread" aria-label={`${unread} finished`} /> : null}
        </button>
      </Tooltip>
      {open ? (
        <Dialog open onClose={() => setOpen(false)} title="Background agents" width="min(640px, 94vw)">
          {list.length === 0 ? (
            <p className="bg-jobs-empty">No background agents have run yet.</p>
          ) : (
            <>
              {finished > 0 ? (
                <div className="bg-jobs-toolbar">
                  <button type="button" className="bg-jobs-clear" onClick={clearFinished}>
                    <Trash2 size={12} /> Clear finished ({finished})
                  </button>
                </div>
              ) : null}
              <ul className="bg-jobs-list">
                {list.map((j) => (
                  <BgJobRow key={j.id} job={j} tools={progress[j.id] || []} onStop={stop} onDelete={del} />
                ))}
              </ul>
            </>
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
  onDelete,
}: {
  job: BackgroundJobDTO;
  tools: ProgressTool[];
  onStop: (jobId: string) => void;
  onDelete: (jobId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const running = job.status === "running";
  const icon = running ? (
    <Spinner size={14} />
  ) : job.status === "failed" ? (
    <XCircle size={14} className="bg-jobs-fail" />
  ) : (
    <CheckCircle2 size={14} className="bg-jobs-ok" />
  );
  const elapsed = fmtElapsed(job.created_at, running ? undefined : job.completed_at);
  const hasResult = !running && !!job.result;
  const hasTools = tools.length > 0;
  // Expandable when there's a result to read OR a live/historical tool feed to watch —
  // so a RUNNING job can now be opened to follow its tool-by-tool activity (ADR 0050
  // Phase 3's deferred "rich live subagent card"), not just finished jobs.
  const canExpand = hasResult || hasTools;
  const recentTools = tools.slice(-3);
  return (
    <li className="bg-jobs-row">
      <div className="bg-jobs-rowhead">
        <button
          type="button"
          className="bg-jobs-rowmain"
          onClick={() => canExpand && setExpanded((v) => !v)}
          aria-expanded={canExpand ? expanded : undefined}
          disabled={!canExpand}
        >
          <span className="bg-jobs-icon">{icon}</span>
          <span className="bg-jobs-meta">
            <span className="bg-jobs-title">
              <strong>{job.subagent_type || "agent"}</strong> — {job.description || "(no description)"}
            </span>
            <span className="bg-jobs-sub">
              {job.status}
              {elapsed ? ` · ${elapsed}` : ""}
              {canExpand ? (expanded ? " · hide details" : hasResult ? " · show result" : " · show activity") : ""}
            </span>
            {running && recentTools.length > 0 ? (
              <span className="bg-jobs-tools">
                {recentTools.map((t) => (
                  <span key={t.id} className={`bg-jobs-tool ${t.error ? "is-err" : t.done ? "is-done" : "is-run"}`}>
                    {t.error ? <XCircle size={11} /> : t.done ? <CheckCircle2 size={11} /> : <Spinner size={11} />} {t.tool}
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
        ) : (
          <button
            type="button"
            className="bg-jobs-stop"
            onClick={() => onDelete(job.id)}
            title="Delete this entry"
            aria-label="Delete"
          >
            <Trash2 size={12} />
          </button>
        )}
      </div>
      {expanded && canExpand ? (
        <div className="bg-jobs-detail">
          {hasTools ? (
            <ToolCardList className="bg-jobs-feed">
              {tools.map((t) => (
                <ToolCard
                  key={t.id}
                  name={t.tool}
                  status={t.error ? "error" : t.done ? "done" : "running"}
                >
                  {t.output ? <ToolSection label="output">{t.output}</ToolSection> : null}
                </ToolCard>
              ))}
            </ToolCardList>
          ) : null}
          {hasResult ? (
            <div className="bg-jobs-result">
              <Markdown>{job.result || ""}</Markdown>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
