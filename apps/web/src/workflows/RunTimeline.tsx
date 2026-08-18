import { Button } from "@protolabsai/ui/primitives";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  CircleDashed,
  Clock,
  Loader2,
  Pause,
  Pencil,
  Play,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { queryKeys, workflowRunQuery, workflowRunsQuery } from "../lib/queries";
import type { WorkflowRunRecord } from "../lib/types";

// The live run timeline — the Studio's "testing" half (ADR 0002). A detached run
// (POST /{name}/start) is watched by polling its durable record: the recipe's step
// graph renders as parallelism lanes, each step flips queued → running → done/failed
// in place with its output expandable as it lands, and a `gate: human` pause surfaces
// its approve/edit/reject actions inline (background resume, so the timeline keeps
// polling through the continuation). Terminal runs render the final envelope — the
// same component doubles as the history inspector for past runs.

type StepView = {
  id: string;
  subagent: string;
  gate?: string;
  status: "queued" | "running" | "done" | "failed" | "gated";
  seconds?: number;
  startedAt?: string;
  output?: string;
};

// Dependency depth → lanes: lane N holds every step whose longest dependency chain
// has N-1 steps before it, so steps sharing a lane are exactly the ones that can run
// in parallel. Cycles can't exist post-validate; the visited guard is a belt.
// Exported for the surface's read-only recipe lanes (same math, no run state).
export function computeLanes(steps: { id: string; depends_on: string[] }[]): string[][] {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();

  function visit(id: string): number {
    const known = depth.get(id);
    if (known != null) return known;
    if (visiting.has(id) || !byId.has(id)) return 0;
    visiting.add(id);
    const deps = byId.get(id)?.depends_on ?? [];
    const d = deps.length ? 1 + Math.max(...deps.map(visit)) : 0;
    visiting.delete(id);
    depth.set(id, d);
    return d;
  }

  const lanes: string[][] = [];
  for (const s of steps) {
    const d = visit(s.id);
    (lanes[d] ??= []).push(s.id);
  }
  return lanes;
}

function stepViews(record: WorkflowRunRecord): StepView[] {
  const lanes = computeLanes(record.steps);
  const ordered = lanes.flat();
  const byId = new Map(record.steps.map((s) => [s.id, s]));
  return ordered.map((id) => {
    const step = byId.get(id)!;
    const meta = record.step_meta?.[id] ?? {};
    const failed = record.failed?.includes(id) || meta.status === "failed";
    const status: StepView["status"] =
      record.status === "paused" && record.pending_step === id
        ? "gated"
        : failed
          ? "failed"
          : meta.status === "done" || id in (record.step_outputs ?? {})
            ? "done"
            : meta.status === "running"
              ? "running"
              : "queued";
    return {
      id,
      subagent: step.subagent,
      gate: step.gate,
      status,
      seconds: meta.seconds,
      startedAt: meta.started_at,
      output: record.step_outputs?.[id],
    };
  });
}

function StatusIcon({ status }: { status: StepView["status"] }) {
  switch (status) {
    case "running":
      return <Loader2 size={14} className="workflow-spin run-step-running" />;
    case "done":
      return <Check size={14} className="run-step-done" />;
    case "failed":
      return <X size={14} className="run-step-failed" />;
    case "gated":
      return <Pause size={14} className="run-step-gated" />;
    default:
      return <CircleDashed size={14} className="run-step-queued" />;
  }
}

function liveSeconds(startedAt: string | undefined, now: number): number | null {
  if (!startedAt) return null;
  const t = Date.parse(startedAt);
  return Number.isFinite(t) ? Math.max(0, (now - t) / 1000) : null;
}

const fmtSecs = (s: number) => (s >= 90 ? `${Math.round(s / 60)}m` : `${s < 10 ? s.toFixed(1) : Math.round(s)}s`);

// The inline gate: the parked step's RENDERED prompt (from the paused-runs view,
// which templates inputs + prior outputs server-side) + approve/edit/reject, resumed
// in the background so this timeline keeps polling through the continuation.
function InlineGate({ record }: { record: WorkflowRunRecord }) {
  const queryClient = useQueryClient();
  const { data } = useQuery(workflowRunsQuery());
  const paused = (data?.runs ?? []).find((r) => r.run_id === record.run_id);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const resume = useMutation({
    mutationFn: (v: { action: "approve" | "edit" | "reject"; edits?: { prompt?: string } }) =>
      api.resumeWorkflowBackground(record.run_id, v),
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflowRuns });
    },
  });

  return (
    <div className="workflow-gate-card run-gate">
      <div className="workflow-gate-head">
        <Pause size={14} />
        <strong>Waiting for approval</strong>
        <span className="workflow-step-sub">{record.pending_step}</span>
      </div>
      {paused ? (
        editing ? (
          <textarea
            className="workflow-gate-edit"
            value={draft}
            rows={6}
            onChange={(event) => setDraft(event.target.value)}
            aria-label="edited prompt"
          />
        ) : (
          <pre className="output-block">{paused.prompt}</pre>
        )
      ) : (
        <p className="run-gate-loading">Loading the step's rendered prompt…</p>
      )}
      {resume.isPending ? (
        <div className="workflow-gate-busy">
          <Loader2 size={16} className="workflow-spin" /> Resuming…
        </div>
      ) : editing ? (
        <div className="panel-actions">
          <Button
            variant="primary"
            type="button"
            onClick={() => resume.mutate({ action: "edit", edits: { prompt: draft } })}
            title="Run with the edited prompt"
          >
            <Check size={14} /> Save &amp; run
          </Button>
          <Button variant="ghost" type="button" onClick={() => setEditing(false)}>
            Cancel
          </Button>
        </div>
      ) : (
        <div className="panel-actions">
          <Button variant="primary" type="button" onClick={() => resume.mutate({ action: "approve" })}>
            <Check size={14} /> Approve
          </Button>
          <Button
            variant="ghost"
            type="button"
            disabled={!paused}
            onClick={() => {
              setDraft(paused?.prompt ?? "");
              setEditing(true);
            }}
          >
            <Pencil size={14} /> Edit
          </Button>
          <Button variant="ghost" type="button" onClick={() => resume.mutate({ action: "reject" })}>
            <X size={14} /> Reject
          </Button>
        </div>
      )}
      {resume.isError ? <p className="workflow-failed">{errMsg(resume.error)}</p> : null}
    </div>
  );
}

export function RunTimeline({ runId, onClose }: { runId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: record, error } = useQuery(workflowRunQuery(runId));
  const active = record?.status === "running" || record?.status === "paused";

  // A ticking clock for live per-step elapsed; idle once the run is terminal.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(t);
  }, [active]);

  // A terminal transition refreshes history + the Pending Gates queue once.
  const status = record?.status;
  useEffect(() => {
    if (status === "done" || status === "failed") {
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflowRunHistory });
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflowRuns });
    }
  }, [status, queryClient]);

  const views = useMemo(() => (record ? stepViews(record) : []), [record]);
  const lanes = useMemo(() => (record ? computeLanes(record.steps) : []), [record]);
  const statusById = useMemo(() => new Map(views.map((v) => [v.id, v.status])), [views]);

  if (error) return <p className="workflow-failed">{errMsg(error)}</p>;
  if (!record) return null;

  return (
    <section className="run-timeline">
      <div className="run-timeline-head">
        <Play size={14} />
        <strong>{record.recipe_name}</strong>
        <span className={`run-status run-status-${record.status}`}>{record.status}</span>
        <span className="run-timeline-when">{ago(record.created_at ?? null)}</span>
        <Button icon variant="ghost" type="button" onClick={onClose} title="Close run view">
          <X size={14} />
        </Button>
      </div>

      {lanes.length > 1 || (lanes[0]?.length ?? 0) > 1 ? (
        <div className="workflow-lanes" aria-label="step order (columns run in parallel)">
          {lanes.map((lane, i) => (
            <div className="workflow-lane" key={i}>
              {lane.map((id) => (
                <span key={id} className={`lane-chip lane-chip-${statusById.get(id) ?? "queued"}`}>
                  {id}
                </span>
              ))}
            </div>
          ))}
        </div>
      ) : null}

      <div className="run-steps">
        {views.map((v) => (
          <details className={`run-step run-step-row-${v.status}`} key={v.id} open={v.status === "failed"}>
            <summary>
              <StatusIcon status={v.status} />
              <strong>{v.id}</strong>
              <span className="workflow-step-sub">{v.subagent}</span>
              {v.gate ? <Pause size={11} className="run-step-gate-mark" aria-label="operator gate" /> : null}
              <span className="run-step-time">
                {v.status === "running" && liveSeconds(v.startedAt, now) != null
                  ? fmtSecs(liveSeconds(v.startedAt, now)!)
                  : v.seconds != null
                    ? fmtSecs(v.seconds)
                    : v.status === "queued"
                      ? "queued"
                      : ""}
              </span>
              {v.output ? <ChevronDown size={12} className="run-step-chevron" /> : null}
            </summary>
            {v.output ? <pre className="output-block">{v.output}</pre> : null}
          </details>
        ))}
      </div>

      {record.status === "paused" && record.pending_step ? <InlineGate record={record} /> : null}

      {record.degraded?.length ? (
        <p className="run-degraded">
          <Clock size={12} /> Timed out (degraded, dependents proceeded): {record.degraded.join(", ")}
        </p>
      ) : null}
      {record.status === "failed" || record.failed?.length ? (
        <p className="workflow-failed">Failed steps: {(record.failed ?? []).join(", ") || "(run error)"}</p>
      ) : null}
      {record.status === "done" && record.output ? (
        <div className="workflow-result">
          <h2>Output</h2>
          <pre className="output-block">{record.output}</pre>
        </div>
      ) : null}
    </section>
  );
}
