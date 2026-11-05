// The run inspector (#4726): one drawer that shows everything recorded for a run, opened from
// anywhere with `openRunInspector(id)` (chat "View run", the telemetry turns table, a
// background job row, the org chart) and from a `?run=<id>` link. It reads
// `GET /api/runs/{id}` and draws the delegation tree `runTree.ts` builds from it.
import "./runinspector.css";

import { StatusDot } from "@protolabsai/ui/data";
import { Drawer } from "@protolabsai/ui/overlays";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, ChevronRight, Copy, ExternalLink, FileText, Link2, Workflow, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { openPromptViewer } from "../chat/PromptViewer";
import { openDocument } from "../docviewer";
import { loadBackgroundReport } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { errMsg, localStamp, ms, tokens, usd } from "../lib/format";
import { runQuery } from "../lib/queries";
import type { TelemetryTurn } from "../lib/types";
import { runDeepLinkUrl } from "../lib/viewDeepLink";
import { traceCellState } from "../telemetry/traceUrl";
import {
  buildRunTree,
  KIND_LABEL,
  outcomeLabel,
  outcomeTone,
  shortRunId,
  spanLabel,
  visibleIds,
  type RunNode,
  type RunTree,
} from "./runTree";
import { closeRunInspector, useRunInspector } from "./store";

export const NO_RUN = "No run recorded under this id. Runs are kept per the retention settings.";

type TraceCtx = { template: string | null; tracingEnabled: boolean };

/** The root-mounted host. One instance, in App. */
export function RunInspector() {
  const runId = useRunInspector((s) => s.runId);
  return (
    <Drawer
      open={!!runId}
      onClose={closeRunInspector}
      width="min(560px, 100vw)"
      className="run-inspector"
      title={
        <>
          <Workflow size={16} aria-hidden /> Run
        </>
      }
    >
      {runId ? <RunInspectorBody key={runId} runId={runId} /> : null}
    </Drawer>
  );
}

/** Exported for the unit harness; the drawer mounts it for the open run id. */
export function RunInspectorBody({ runId }: { runId: string }) {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery(runQuery(runId));
  const tree = useMemo(() => (data ? buildRunTree(data) : null), [data]);

  if (isLoading) {
    return (
      <p className="run-inspector__muted" data-testid="run-loading">
        Loading run…
      </p>
    );
  }
  if (isError || !data || !tree) {
    return (
      <div className="run-inspector__state" role="alert" data-testid="run-error">
        <p>Couldn't load this run. {error ? errMsg(error) : ""}</p>
        <Button size="sm" onClick={() => void refetch()} loading={isFetching}>
          Retry
        </Button>
      </div>
    );
  }
  const ctx: TraceCtx = { template: data.langfuse_trace_url_template ?? null, tracingEnabled: data.tracing_enabled ?? true };
  return (
    <div className="run-inspector__body" data-testid="run-inspector">
      <RunHeader tree={tree} asked={runId} />
      {tree.empty ? (
        <p className="run-inspector__muted" data-testid="run-empty">
          {NO_RUN}
        </p>
      ) : (
        <RunTreeView roots={tree.roots} ctx={ctx} />
      )}
      <div className="run-inspector__foot">
        <CopyButton text={runDeepLinkUrl(tree.runId || runId)} label="Copy link" testId="run-copy-link" icon={<Link2 size={13} aria-hidden />} />
      </div>
    </div>
  );
}

function RunHeader({ tree, asked }: { tree: RunTree; asked: string }) {
  const resolvedNode = tree.resolvedFrom ? findNode(tree.roots, (n) => n.taskId === tree.resolvedFrom || n.id === tree.resolvedFrom) : null;
  const via = !resolvedNode
    ? "id"
    : resolvedNode.id === tree.resolvedFrom
      ? KIND_LABEL[resolvedNode.kind]
      : resolvedNode.kind === "background"
        ? "job"
        : resolvedNode.kind === "workflow"
          ? "workflow run"
          : resolvedNode.kind === "coder"
            ? "coder task"
            : KIND_LABEL[resolvedNode.kind];
  const id = tree.runId || asked;
  return (
    <header className="run-inspector__head" data-testid="run-header">
      <div className="run-inspector__id">
        <code title={id}>{shortRunId(id)}</code>
        <CopyButton text={id} label="Copy run id" iconOnly />
      </div>
      {tree.empty ? null : (
        <p className="run-inspector__summary">
          <Badge status={outcomeTone(tree.outcome)}>{outcomeLabel(tree.outcome) || "unknown"}</Badge>
          <span>{tree.costUsd == null ? "cost unmeasured" : usd(tree.costUsd)}</span>
          <span aria-label="wall-clock span">{spanLabel(tree.spanMs)}</span>
          {tree.liveCount ? (
            <span className="run-inspector__live" data-testid="run-live-count">
              <StatusDot status="info" pulse /> {tree.liveCount} in flight
            </span>
          ) : null}
        </p>
      )}
      {tree.completion ? (
        <p className="run-inspector__meta" data-testid="run-completion">
          The completion check says {tree.completion}
          {tree.nudges ? ` after ${tree.nudges} nudge${tree.nudges === 1 ? "" : "s"}` : ""}.
        </p>
      ) : null}
      {tree.shortfalls ? (
        <p className="run-inspector__meta run-inspector__warn" data-testid="run-shortfalls">
          {tree.shortfalls === 1 ? "One subagent" : `${tree.shortfalls} subagents`} came back without the full deliverable.
        </p>
      ) : null}
      {tree.resolvedFrom ? (
        <p className="run-inspector__meta" data-testid="run-resolved-from">
          Opened from {via} <code title={tree.resolvedFrom}>{shortRunId(tree.resolvedFrom)}</code>.
        </p>
      ) : null}
    </header>
  );
}

function findNode(nodes: RunNode[], pred: (n: RunNode) => boolean): RunNode | null {
  for (const n of nodes) {
    if (pred(n)) return n;
    const hit = findNode(n.children, pred);
    if (hit) return hit;
  }
  return null;
}

function hasDetail(n: RunNode): boolean {
  return n.turns.length > 0 || n.kind === "background" || n.what.length > 80 || !!n.taskId;
}

/** Expanded by default: anything with children (so the tree reads at a glance) and any
 *  node that failed or came back short (so the why is already showing). */
function defaultExpanded(n: RunNode): boolean {
  return n.children.length > 0 || n.outcome === "failed" || n.completion === "incomplete" || n.completion === "partial";
}

// An accessible tree (WAI-ARIA tree view): role=tree / treeitem / group, aria-expanded on
// every node that has something to expand, one roving tab stop, and the arrow keys walk the
// visible nodes (Up/Down), open or enter a node (Right), close or leave it (Left), Home/End.
export function RunTreeView({ roots, ctx }: { roots: RunNode[]; ctx: TraceCtx }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const isOpen = useCallback(
    (id: string, n?: RunNode) => open[id] ?? (n ? defaultExpanded(n) : false),
    [open],
  );
  const index = useMemo(() => {
    const m = new Map<string, { node: RunNode; parent: string }>();
    const walk = (ns: RunNode[], parent: string) => ns.forEach((n) => (m.set(n.id, { node: n, parent }), walk(n.children, n.id)));
    walk(roots, "");
    return m;
  }, [roots]);
  const [focused, setFocused] = useState<string>(roots[0]?.id ?? "");
  const treeRef = useRef<HTMLUListElement>(null);
  const tabStop = index.has(focused) ? focused : roots[0]?.id ?? "";

  const focusNode = (id: string) => {
    setFocused(id);
    // Matched by dataset, not a selector: an id is free text (a `board:` run id has a colon).
    const items = treeRef.current?.querySelectorAll<HTMLElement>('[role="treeitem"]') ?? [];
    for (const el of items) if (el.dataset.nodeId === id) return el.focus();
  };
  const toggle = (n: RunNode) => setOpen((o) => ({ ...o, [n.id]: !isOpen(n.id, n) }));

  const onKeyDown = (e: KeyboardEvent<HTMLUListElement>) => {
    const target = e.target as HTMLElement;
    if (target.getAttribute("role") !== "treeitem") return; // a button inside a node keeps its keys
    const id = target.dataset.nodeId || "";
    const entry = index.get(id);
    if (!entry) return;
    const order = visibleIds(roots, (x) => isOpen(x, index.get(x)?.node));
    const at = order.indexOf(id);
    const expandable = entry.node.children.length > 0 || hasDetail(entry.node);
    const expanded = isOpen(id, entry.node);
    let handled = true;
    switch (e.key) {
      case "ArrowDown":
        if (at < order.length - 1) focusNode(order[at + 1]);
        break;
      case "ArrowUp":
        if (at > 0) focusNode(order[at - 1]);
        break;
      case "Home":
        focusNode(order[0]);
        break;
      case "End":
        focusNode(order[order.length - 1]);
        break;
      case "ArrowRight":
        if (expandable && !expanded) toggle(entry.node);
        else if (expanded && entry.node.children.length) focusNode(entry.node.children[0].id);
        break;
      case "ArrowLeft":
        if (expandable && expanded) toggle(entry.node);
        else if (entry.parent) focusNode(entry.parent);
        break;
      case "Enter":
      case " ":
        if (expandable) toggle(entry.node);
        break;
      default:
        handled = false;
    }
    if (handled) e.preventDefault();
  };

  return (
    <ul className="run-tree" role="tree" aria-label="Run tree" ref={treeRef} onKeyDown={onKeyDown} data-testid="run-tree">
      {roots.map((n) => (
        <RunTreeItem key={n.id} node={n} level={1} isOpen={isOpen} toggle={toggle} tabStop={tabStop} onFocusNode={setFocused} ctx={ctx} />
      ))}
    </ul>
  );
}

function RunTreeItem({
  node,
  level,
  isOpen,
  toggle,
  tabStop,
  onFocusNode,
  ctx,
}: {
  node: RunNode;
  level: number;
  isOpen: (id: string, n?: RunNode) => boolean;
  toggle: (n: RunNode) => void;
  tabStop: string;
  onFocusNode: (id: string) => void;
  ctx: TraceCtx;
}) {
  const expandable = node.children.length > 0 || hasDetail(node);
  const expanded = expandable && isOpen(node.id, node);
  const short = node.completion === "incomplete" || node.completion === "partial";
  return (
    <li
      role="treeitem"
      aria-level={level}
      aria-expanded={expandable ? expanded : undefined}
      aria-label={`${KIND_LABEL[node.kind]} ${node.name}, ${outcomeLabel(node.outcome)}`}
      tabIndex={tabStop === node.id ? 0 : -1}
      data-node-id={node.id}
      data-testid="run-node"
      className={`run-node run-node--${node.kind}${node.outcome === "failed" ? " run-node--failed" : ""}`}
      onFocus={(e) => {
        if (e.target === e.currentTarget) onFocusNode(node.id);
      }}
    >
      <div
        className="run-node__row"
        onClick={() => {
          if (expandable) toggle(node);
          onFocusNode(node.id);
        }}
      >
        <span className="run-node__chev" aria-hidden>
          {expandable ? expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} /> : null}
        </span>
        <span className="run-node__kind">{KIND_LABEL[node.kind]}</span>
        <span className="run-node__name">{node.name || "(unnamed)"}</span>
        <span className="run-node__facts">
          {node.costUsd != null ? <span>{usd(node.costUsd)}</span> : null}
          {node.durationMs ? <span>{ms(node.durationMs)}</span> : null}
          {node.live ? (
            <span className="run-node__live" data-testid="run-node-live">
              <StatusDot status="info" pulse label="live" />
            </span>
          ) : (
            <Badge status={outcomeTone(node.outcome)}>{outcomeLabel(node.outcome) || "—"}</Badge>
          )}
          {node.completion ? (
            <span className={short ? "run-node__completion run-node__completion--short" : "run-node__completion"} data-testid="run-node-completion">
              <Badge status={short ? "warning" : "success"}>
                {node.completion}
                {node.nudges ? ` · ${node.nudges} nudge${node.nudges === 1 ? "" : "s"}` : ""}
              </Badge>
            </span>
          ) : null}
        </span>
      </div>
      {node.what ? (
        <p className="run-node__what" title={node.what}>
          {node.what}
        </p>
      ) : null}
      {node.live && node.progress ? <p className="run-node__progress">{node.progress}</p> : null}
      {node.error ? <ClippedError text={node.error} /> : null}
      {expanded && hasDetail(node) ? <NodeDetail node={node} ctx={ctx} /> : null}
      {expanded && node.children.length ? (
        <ul role="group" className="run-tree__group">
          {node.children.map((c) => (
            <RunTreeItem key={c.id} node={c} level={level + 1} isOpen={isOpen} toggle={toggle} tabStop={tabStop} onFocusNode={onFocusNode} ctx={ctx} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/** The error on one clipped line; a tap or click shows all of it (a touch screen has no
 *  hover tooltip). Same affordance as the telemetry table's TurnStateCell. */
export function ClippedError({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <button
      type="button"
      className="run-node__error"
      title={text}
      aria-expanded={expanded}
      data-expanded={expanded ? "" : undefined}
      onClick={() => setExpanded((v) => !v)}
      data-testid="run-node-error"
    >
      {text}
    </button>
  );
}

function NodeDetail({ node, ctx }: { node: RunNode; ctx: TraceCtx }) {
  return (
    <div className="run-node__detail" data-testid="run-node-detail">
      {node.what.length > 80 ? <p className="run-node__what-full">{node.what}</p> : null}
      {node.taskId || node.startedAt ? (
        <p className="run-node__ids">
          {node.taskId ? (
            <>
              task <code>{node.taskId}</code>
            </>
          ) : null}
          {node.startedAt ? <span> · started {localStamp(new Date(node.startedAt).toISOString())}</span> : null}
        </p>
      ) : null}
      {node.turns.map((t, i) => (
        <div key={t.row_id ?? `${t.task_id}-${i}`} className="run-node__turn" data-testid="run-node-turn">
          <p>
            <strong>{t.model || "model unknown"}</strong> · {tokens(t.input_tokens || 0)}→{tokens(t.output_tokens || 0)} tokens · {t.llm_calls ?? 0} llm /{" "}
            {t.tool_calls ?? 0} tool calls · {usd(t.cost_usd || 0)} · {ms(t.duration_ms || 0)}
            {node.turns.length > 1 ? ` · ${t.state}` : ""}
          </p>
          <div className="run-node__links">
            <TurnLinks turn={t} ctx={ctx} />
          </div>
        </div>
      ))}
      {node.kind === "background" && node.taskId ? (
        <div className="run-node__links">
          <Button
            size="sm"
            variant="ghost"
            data-testid="run-node-job"
            onClick={() =>
              openDocument({
                title: node.name || "Background agent",
                subtitle: "Background agent report",
                load: () => loadBackgroundReport(node.taskId),
              })
            }
          >
            <FileText size={13} aria-hidden /> Job report
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** The trace (same four-way decision as the telemetry table's Trace cell) and the prompts. */
function TurnLinks({ turn, ctx }: { turn: TelemetryTurn; ctx: TraceCtx }) {
  const trace = traceCellState(ctx.template, turn.trace_id, ctx.tracingEnabled);
  return (
    <>
      {trace.kind === "link" ? (
        <a className="run-link" href={trace.href} target="_blank" rel="noreferrer noopener" data-testid="run-trace-link" title={`Open trace ${turn.trace_id} in Langfuse`}>
          <ExternalLink size={13} aria-hidden /> Trace
        </a>
      ) : trace.kind === "copy" ? (
        <CopyButton text={trace.traceId} label={`Trace ${trace.short}`} testId="run-trace-copy" />
      ) : null}
      {turn.task_id ? (
        <Button size="sm" variant="ghost" data-testid="run-prompts" onClick={() => openPromptViewer(turn.task_id, turn.session_id || undefined)}>
          <FileText size={13} aria-hidden /> Prompts
        </Button>
      ) : null}
    </>
  );
}

export function CopyButton({ text, label, testId, icon, iconOnly }: { text: string; label: string; testId?: string; icon?: ReactNode; iconOnly?: boolean }) {
  const [outcome, setOutcome] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (outcome === "idle") return;
    const t = setTimeout(() => setOutcome("idle"), outcome === "copied" ? 1500 : 3000);
    return () => clearTimeout(t);
  }, [outcome]);
  const copied = outcome === "copied";
  const failed = outcome === "failed";
  return (
    <Button
      size="sm"
      variant="ghost"
      icon={iconOnly}
      aria-label={iconOnly ? label : undefined}
      title={failed ? "Copy failed" : label}
      data-testid={testId}
      data-copy-outcome={outcome}
      onClick={() => {
        void copyText(text).then((ok) => setOutcome(ok ? "copied" : "failed"));
      }}
    >
      {copied ? <Check size={13} aria-hidden /> : failed ? <XCircle size={13} aria-hidden /> : icon ?? <Copy size={13} aria-hidden />}
      {iconOnly ? null : copied ? " Copied" : failed ? " Copy failed" : ` ${label}`}
    </Button>
  );
}
