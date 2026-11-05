// The run inspector's model (#4726): one tree of nodes built from what `GET /api/runs/{id}`
// returns. Pure, so the shape is unit-tested without rendering.
//
// Three inputs, one node type:
//   * `edges`, the durable `delegations` rows, oldest first. Nesting is `edge_uuid` /
//     `parent_uuid`; a row whose parent isn't in the run hangs at the top (the ledger's own
//     rule, observability/ledger_store.py `run`).
//   * `live`, the edges in flight, in the live registry's shape. A live `id` IS the durable
//     `edge_uuid` the row will carry when it closes. Most live edges have no row yet; a
//     background job has both (its row is written at spawn), so the two merge into one node
//     and the live side wins for status and progress.
//   * `turns`, telemetry rows. A turn attaches to the edge whose `task_id` it carries, or,
//     for a background job's own turn, the job whose id its `background:<job>` session
//     names. The turn whose task id IS the run id is the root turn, the node the run hangs
//     under. Any other turn that matches nothing lands in a "turns" group, so nothing the
//     run recorded goes missing from the picture.
import type { RunEdgeRow, RunLiveEdge, RunRecord, TelemetryTurn } from "../lib/types";

export type RunNodeKind = "turn" | "turns" | "subagent" | "background" | "coder" | "delegate" | "model" | "workflow" | "other";

export type RunNode = {
  /** The edge uuid (durable or live), or a synthetic id for the turn and group nodes. */
  id: string;
  parentId: string;
  kind: RunNodeKind;
  /** The raw ledger `to_kind` ("" for a synthetic node). */
  toKind: string;
  name: string;
  what: string;
  /** ok / failed / cancelled for a finished edge, `running` for a live one, a turn's state
   *  (completed / failed / canceled / …) for a turn node. */
  outcome: string;
  error: string;
  costUsd: number | null;
  durationMs: number;
  /** Epoch ms, or null when unknown. */
  startedAt: number | null;
  endedAt: number | null;
  completion: string;
  nudges: number;
  taskId: string;
  sessionId: string;
  origin: string;
  live: boolean;
  /** One line of what a live edge is doing right now, from its progress snapshot. */
  progress: string;
  turns: TelemetryTurn[];
  children: RunNode[];
};

export type RunTree = {
  runId: string;
  resolvedFrom: string;
  /** The top of the tree: the root turn (holding every top-level edge) when it was
   *  recorded, else the top-level edges themselves, then the "turns" group if any. */
  roots: RunNode[];
  /** How many delegation edges the run has (live and durable, merged). */
  nodeCount: number;
  liveCount: number;
  outcome: string;
  /** Sum of every priced edge and turn; null when nothing in the run carried a cost. */
  costUsd: number | null;
  /** First start to last end, in ms; null when the run has no timestamps. */
  spanMs: number | null;
  /** The root edge's completion verdict (a run rooted at a subagent or workflow edge). */
  completion: string;
  nudges: number;
  /** Subagents whose deliverable didn't arrive (`incomplete`) or that hard-stopped (`partial`). */
  shortfalls: number;
  /** Edges anywhere in the run the completion check ruled on (complete, incomplete or partial).
   *  A chat-rooted run has no root verdict (the lead turn isn't checked), so this roll-up is
   *  what its header can say about the check. */
  checked: number;
  /** Checked edges that passed only after the check nudged them. */
  rescued: number;
  empty: boolean;
};

const CHECKED = new Set(["complete", "incomplete", "partial"]);

const ms = (iso: string | null | undefined): number | null => {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
};

export function nodeKind(toKind: string, origin: string): RunNodeKind {
  if (origin === "background") return "background";
  switch (toKind) {
    case "subagent":
      return "subagent";
    case "acp":
      return "coder";
    case "a2a":
      return "delegate";
    case "openai":
      return "model";
    case "workflow":
      return "workflow";
    default:
      return "other";
  }
}

export const KIND_LABEL: Record<RunNodeKind, string> = {
  turn: "turn",
  turns: "turns",
  subagent: "subagent",
  background: "background job",
  coder: "coder",
  delegate: "a2a delegate",
  model: "model",
  workflow: "workflow",
  other: "delegation",
};

/** One readable line of a live edge's progress snapshot (graph.delegate_progress). */
export function progressLine(p: Record<string, unknown> | null | undefined): string {
  if (!p || typeof p !== "object") return "";
  const tool = p.current_tool as { name?: unknown; status?: unknown } | undefined;
  if (tool && typeof tool.name === "string" && tool.status === "running") return `running ${tool.name}`;
  if (typeof p.text === "string" && p.text.trim()) return p.text.trim().slice(0, 200);
  const count = Number(p.tool_count);
  if (Number.isFinite(count) && count > 0) return `${Math.floor(count)} tool call${count === 1 ? "" : "s"}`;
  return "";
}

function fromRow(r: RunEdgeRow): RunNode {
  const at = ms(r.at);
  const dur = Number(r.duration_ms) || 0;
  // A dispatch row is written when it ENDS; a background job's row is written when it is
  // SPAWNED and its duration settled onto it later.
  const background = r.origin === "background";
  const startedAt = at == null ? null : background ? at : at - dur;
  const endedAt = at == null ? null : background ? (dur ? at + dur : null) : at;
  return {
    id: r.edge_uuid || `row:${r.edge_id}`,
    parentId: r.parent_uuid || "",
    kind: nodeKind(r.to_kind, r.origin),
    toKind: r.to_kind,
    name: r.to_name || "",
    what: r.what || "",
    outcome: r.outcome || "ok",
    error: (r.error || "").trim(),
    costUsd: r.cost_usd == null ? null : Number(r.cost_usd),
    durationMs: dur,
    startedAt,
    endedAt,
    completion: r.completion || "",
    nudges: Number(r.nudges) || 0,
    taskId: r.task_id || "",
    sessionId: r.session_id || "",
    origin: r.origin || "",
    live: false,
    progress: "",
    turns: [],
    children: [],
  };
}

function fromLive(e: RunLiveEdge, now: number): RunNode {
  const started = Number(e.started_at) ? Number(e.started_at) * 1000 : null;
  return {
    id: e.id,
    parentId: e.parent_id || "",
    kind: nodeKind(e.to_kind, e.origin || ""),
    toKind: e.to_kind,
    name: e.to_name || "",
    what: e.what || "",
    outcome: "running",
    error: "",
    costUsd: null,
    durationMs: started == null ? 0 : Math.max(0, now - started),
    startedAt: started,
    endedAt: null,
    completion: e.completion || "",
    nudges: Number(e.nudges) || 0,
    taskId: e.task_id || "",
    sessionId: e.session_id || "",
    origin: e.origin || "",
    live: true,
    progress: progressLine(e.progress),
    turns: [],
    children: [],
  };
}

function turnNode(id: string, kind: "turn" | "turns", name: string, turns: TelemetryTurn[]): RunNode {
  const t = turns[0];
  const start = Math.min(...turns.map((x) => ms(x.created_at) ?? Infinity));
  const end = Math.max(...turns.map((x) => ms(x.ended_at) ?? -Infinity));
  const priced = turns.filter((x) => x.cost_usd != null);
  const failed = turns.find((x) => x.state !== "completed");
  return {
    id,
    parentId: "",
    kind,
    toKind: "",
    name,
    what: "",
    outcome: kind === "turn" ? t?.state || "" : failed ? failed.state : "completed",
    error: kind === "turn" && t?.state !== "completed" ? (t?.error || "").trim() : "",
    costUsd: priced.length ? priced.reduce((s, x) => s + (Number(x.cost_usd) || 0), 0) : null,
    durationMs: turns.reduce((s, x) => s + (Number(x.duration_ms) || 0), 0),
    startedAt: Number.isFinite(start) ? start : null,
    endedAt: Number.isFinite(end) ? end : null,
    completion: "",
    nudges: 0,
    taskId: kind === "turn" ? t?.task_id || "" : "",
    sessionId: kind === "turn" ? t?.session_id || "" : "",
    origin: "",
    live: false,
    progress: "",
    turns,
    children: [],
  };
}

/** Normalize the live and durable edges into one node per edge uuid. */
export function normalizeEdges(edges: RunEdgeRow[], live: RunLiveEdge[], now = Date.now()): RunNode[] {
  const byId = new Map<string, RunNode>();
  for (const r of edges) byId.set(r.edge_uuid || `row:${r.edge_id}`, fromRow(r));
  for (const e of live) {
    const l = fromLive(e, now);
    const row = byId.get(e.id);
    // Durable row + live edge = a background job still running: keep the row's facts, take
    // the live status. Its row's `ok` only ever meant "dispatched".
    byId.set(e.id, row ? { ...row, outcome: "running", live: true, progress: l.progress, endedAt: null,
      durationMs: l.durationMs || row.durationMs, completion: l.completion || row.completion,
      nudges: l.nudges || row.nudges, taskId: row.taskId || l.taskId } : l);
  }
  return [...byId.values()];
}

const sortByStart = (a: RunNode, b: RunNode) => (a.startedAt ?? Infinity) - (b.startedAt ?? Infinity);

export function buildRunTree(run: RunRecord, now = Date.now()): RunTree {
  const runId = run.run_id;
  const nodes = normalizeEdges(run.edges ?? [], run.live ?? [], now);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  // Attach turns. Task ids aren't unique (a HITL park/resume is two legs under one), so a
  // turn joins the first edge that claims it and every leg of it lands there.
  const byTask = new Map<string, RunNode>();
  for (const n of nodes) if (n.taskId && !byTask.has(n.taskId)) byTask.set(n.taskId, n);
  const rootTurns: TelemetryTurn[] = [];
  const loose: TelemetryTurn[] = [];
  for (const t of run.turns ?? []) {
    const bg = t.session_id?.startsWith("background:") ? t.session_id.slice("background:".length) : "";
    const owner = byTask.get(t.task_id) ?? (bg ? byTask.get(bg) : undefined);
    if (owner) owner.turns.push(t);
    else if (t.task_id === runId) rootTurns.push(t);
    else loose.push(t);
  }

  // Nest. A parent that isn't in this run (an older row, an edge pruned by retention)
  // leaves its child at the top rather than dropping it.
  const top: RunNode[] = [];
  for (const n of nodes) {
    const parent = n.parentId && n.parentId !== n.id ? byId.get(n.parentId) : undefined;
    if (parent) parent.children.push(n);
    else top.push(n);
  }
  for (const n of nodes) n.children.sort(sortByStart);
  top.sort(sortByStart);

  const roots: RunNode[] = [];
  if (rootTurns.length) {
    const fromAgent = (run.edges ?? []).find((r) => !r.parent_uuid)?.from_agent || "";
    const root = turnNode(`turn:${runId}`, "turn", fromAgent || "this agent", rootTurns);
    root.children = top;
    for (const c of top) c.parentId = root.id;
    roots.push(root);
  } else {
    roots.push(...top);
  }
  if (loose.length) roots.push(turnNode(`turns:${runId}`, "turns", `${loose.length} more turn${loose.length === 1 ? "" : "s"}`, loose));

  // Header facts. Null cost means nothing in the run carried one, not that it was free.
  const anyPriced = nodes.some((n) => n.costUsd != null) || (run.turns ?? []).some((t) => t.cost_usd != null);
  const costUsd = anyPriced ? totalCost(nodes, run.turns ?? []) : null;
  const starts = [...nodes.map((n) => n.startedAt), ...(run.turns ?? []).map((t) => ms(t.created_at))].filter((x): x is number => x != null);
  const liveCount = nodes.filter((n) => n.live).length;
  const ends = [...nodes.map((n) => (n.live ? now : n.endedAt)), ...(run.turns ?? []).map((t) => ms(t.ended_at))].filter((x): x is number => x != null);
  const spanMs = starts.length && ends.length ? Math.max(0, Math.max(...ends) - Math.min(...starts)) : null;
  const rootEdge = byId.get(runId);
  const rootTurn = rootTurns[rootTurns.length - 1];
  const outcome = liveCount
    ? "running"
    : rootTurn
      ? rootTurn.state
      : rootEdge
        ? rootEdge.outcome
        : nodes.some((n) => n.outcome === "failed")
          ? "failed"
          : nodes.some((n) => n.outcome === "cancelled")
            ? "cancelled"
            : nodes.length || (run.turns ?? []).length
              ? "ok"
              : "";

  return {
    runId,
    resolvedFrom: run.resolved_from || "",
    roots,
    nodeCount: nodes.length,
    liveCount,
    outcome,
    costUsd,
    spanMs,
    completion: rootEdge?.completion || "",
    nudges: rootEdge?.nudges || 0,
    shortfalls: nodes.filter((n) => n.completion === "incomplete" || n.completion === "partial").length,
    checked: nodes.filter((n) => CHECKED.has(n.completion)).length,
    rescued: nodes.filter((n) => n.completion === "complete" && n.nudges > 0).length,
    empty: !nodes.length && !(run.turns ?? []).length,
  };
}

/** An edge with a turn attached is priced by its row when the row carries a cost; otherwise
 *  its turn's cost is the measurement. Summing both would double-count, so a turn's cost
 *  counts only when its edge has none. */
function totalCost(nodes: RunNode[], turns: TelemetryTurn[]): number {
  let sum = 0;
  const claimed = new Set<TelemetryTurn>();
  for (const n of nodes) {
    if (n.costUsd != null) {
      sum += n.costUsd;
      n.turns.forEach((t) => claimed.add(t));
    }
  }
  for (const t of turns) if (!claimed.has(t) && t.cost_usd != null) sum += Number(t.cost_usd) || 0;
  return sum;
}

/** The node ids in depth-first display order, skipping the children of collapsed nodes —
 *  what arrow-key navigation walks. */
export function visibleIds(roots: RunNode[], expanded: (id: string) => boolean): string[] {
  const out: string[] = [];
  const walk = (ns: RunNode[]) => {
    for (const n of ns) {
      out.push(n.id);
      if (n.children.length && expanded(n.id)) walk(n.children);
    }
  };
  walk(roots);
  return out;
}

/** One word per outcome in the drawer. The ledger says `ok` and telemetry says
 *  `completed` for the same thing, and the two spell cancel differently. */
export function outcomeLabel(outcome: string): string {
  if (outcome === "ok") return "completed";
  if (outcome === "canceled") return "cancelled";
  return outcome;
}

/** Badge tone for an outcome or turn state. */
export function outcomeTone(outcome: string): "success" | "error" | "warning" | "info" | "neutral" {
  if (outcome === "ok" || outcome === "completed") return "success";
  if (outcome === "failed") return "error";
  if (outcome === "running") return "info";
  if (outcome === "cancelled" || outcome === "canceled" || outcome === "input-required") return "warning";
  return "neutral";
}

/** Short run id for the header: `7f3a9c1e…`, or a `board:` id kept whole when short. */
export function shortRunId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

/** "3m12s" / "42s" / "850ms" / "1h04m". */
export function spanLabel(n: number | null): string {
  if (n == null) return "—";
  if (n < 1000) return `${Math.round(n)}ms`;
  const s = Math.round(n / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`;
}
