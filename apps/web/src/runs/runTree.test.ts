// @vitest-environment node
import { describe, expect, it } from "vitest";

import type { RunEdgeRow, RunLiveEdge, RunRecord, TelemetryTurn } from "../lib/types";
import { buildRunTree, normalizeEdges, outcomeLabel, progressLine, shortRunId, spanLabel, visibleIds, type RunNode } from "./runTree";

// The run inspector's tree (#4726), built from `GET /api/runs/{id}`: durable rows nest by
// edge_uuid/parent_uuid, live edges merge in by id, and turns attach by task id.

const T0 = Date.parse("2026-11-01T10:00:00Z");
const iso = (sec: number) => new Date(T0 + sec * 1000).toISOString();

function row(p: Partial<RunEdgeRow>): RunEdgeRow {
  return {
    edge_id: 1, at: iso(10), from_agent: "console", to_kind: "subagent", to_name: "research", what: "dig",
    session_id: "s1", task_id: "", outcome: "ok", error: "", duration_ms: 5000, cost_usd: null, origin: "task",
    edge_uuid: "e1", parent_uuid: "", run_id: "task-1", completion: "", nudges: 0, ...p,
  };
}
function live(p: Partial<RunLiveEdge>): RunLiveEdge {
  return { id: "l1", parent_id: "", run_id: "task-1", to_kind: "workflow", to_name: "review", started_at: (T0 + 20_000) / 1000, ...p };
}
function turn(p: Partial<TelemetryTurn>): TelemetryTurn {
  return {
    task_id: "task-1", session_id: "s1", state: "completed", success: 1, model: "m", input_tokens: 10, output_tokens: 5,
    total_tokens: 15, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, cost_usd: 0.08, duration_ms: 1000,
    llm_calls: 2, tool_calls: 3, created_at: iso(0), ended_at: iso(30), run_id: "task-1", ...p,
  };
}
function run(p: Partial<RunRecord>): RunRecord {
  return { run_id: "task-1", resolved_from: "", edges: [], turns: [], live: [], ...p };
}
const names = (ns: RunNode[]): unknown[] => ns.map((n) => (n.children.length ? [n.name, names(n.children)] : n.name));

describe("normalizeEdges", () => {
  it("turns a live edge into the same node shape, running, with its progress line", () => {
    const [n] = normalizeEdges([], [live({ progress: { current_tool: { name: "grep", status: "running" } } })], T0 + 25_000);
    expect(n).toMatchObject({ id: "l1", kind: "workflow", outcome: "running", live: true, progress: "running grep", durationMs: 5000, startedAt: T0 + 20_000, endedAt: null });
  });

  it("merges a background job's spawn row with its live edge: one node, the live status wins", () => {
    const nodes = normalizeEdges(
      [row({ edge_uuid: "j1", origin: "background", task_id: "bg-1", outcome: "ok", duration_ms: 0, at: iso(5) })],
      [live({ id: "j1", to_kind: "subagent", origin: "background", task_id: "bg-1", started_at: (T0 + 5000) / 1000 })],
      T0 + 15_000,
    );
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ id: "j1", kind: "background", outcome: "running", live: true, taskId: "bg-1", durationMs: 10_000 });
  });

  it("dates a dispatch row from its end and a background row from its spawn", () => {
    const [d, b] = normalizeEdges([row({ edge_uuid: "d" }), row({ edge_uuid: "b", origin: "background", duration_ms: 4000 })], []);
    expect([d.startedAt, d.endedAt]).toEqual([T0 + 5000, T0 + 10_000]);
    expect([b.startedAt, b.endedAt]).toEqual([T0 + 10_000, T0 + 14_000]);
  });
});

describe("buildRunTree", () => {
  it("nests edges under the root turn by parent_uuid, oldest first, with live edges in place", () => {
    const tree = buildRunTree(
      run({
        edges: [
          row({ edge_uuid: "e1", to_name: "research", at: iso(40), duration_ms: 35_000 }),
          row({ edge_uuid: "e2", parent_uuid: "e1", to_kind: "acp", to_name: "codex", outcome: "failed", error: "timed out after 600s", at: iso(30), duration_ms: 20_000 }),
        ],
        live: [live({ id: "l1", parent_id: "e1", to_name: "review" })],
        turns: [turn({})],
      }),
      T0 + 60_000,
    );
    expect(names(tree.roots)).toEqual([["console", [["research", ["codex", "review"]]]]]);
    expect(tree.roots[0].kind).toBe("turn");
    const codex = tree.roots[0].children[0].children[0];
    expect(codex).toMatchObject({ kind: "coder", outcome: "failed", error: "timed out after 600s" });
    expect(tree.liveCount).toBe(1);
    expect(tree.outcome).toBe("running");
    expect(tree.spanMs).toBe(60_000); // first start (the turn, t0) to now (a live edge)
  });

  it("hangs an orphan (its parent isn't in the run) at the top instead of dropping it", () => {
    const tree = buildRunTree(run({ edges: [row({ edge_uuid: "e1" }), row({ edge_uuid: "e9", parent_uuid: "gone", to_name: "orphan" })] }));
    expect(names(tree.roots)).toEqual(["research", "orphan"]);
  });

  it("attaches turns to the edge with their task id, a job's turn by its session, and keeps the rest", () => {
    const tree = buildRunTree(
      run({
        edges: [
          row({ edge_uuid: "c1", to_kind: "acp", to_name: "codex", task_id: "coder:codex:abc" }),
          row({ edge_uuid: "j1", to_name: "researcher", origin: "background", task_id: "bg-7" }),
        ],
        turns: [
          turn({ task_id: "task-1" }),
          turn({ task_id: "coder:codex:abc", model: "gpt" }),
          turn({ task_id: "a2a-77", session_id: "background:bg-7", model: "job-model" }),
          turn({ task_id: "stray", model: "who" }),
        ],
      }),
    );
    const [root, group] = tree.roots;
    expect(root.turns.map((t) => t.task_id)).toEqual(["task-1"]);
    const byName = Object.fromEntries(root.children.map((n) => [n.name, n]));
    expect(byName.codex.turns.map((t) => t.model)).toEqual(["gpt"]);
    expect(byName.researcher.turns.map((t) => t.model)).toEqual(["job-model"]);
    expect(group).toMatchObject({ kind: "turns", name: "1 more turn" });
    expect(group.turns.map((t) => t.model)).toEqual(["who"]);
  });

  it("totals cost without counting an edge's turn twice, and is null when nothing was priced", () => {
    const tree = buildRunTree(
      run({
        edges: [row({ edge_uuid: "c1", task_id: "k", cost_usd: 0.13 }), row({ edge_uuid: "c2", task_id: "u", cost_usd: null })],
        turns: [turn({ cost_usd: 0.08 }), turn({ task_id: "k", cost_usd: 0.13 }), turn({ task_id: "u", cost_usd: 0.21 })],
      }),
    );
    expect(tree.costUsd).toBeCloseTo(0.42);
    expect(buildRunTree(run({ edges: [row({})] })).costUsd).toBeNull();
  });

  it("reports the root edge's completion, counts shortfalls, and the root turn's outcome", () => {
    const tree = buildRunTree(
      run({
        run_id: "e1",
        edges: [
          row({ edge_uuid: "e1", run_id: "e1", completion: "complete", nudges: 1 }),
          row({ edge_uuid: "e2", parent_uuid: "e1", run_id: "e1", completion: "incomplete" }),
          row({ edge_uuid: "e3", parent_uuid: "e1", run_id: "e1", completion: "partial", outcome: "failed" }),
        ],
      }),
    );
    expect([tree.completion, tree.nudges, tree.shortfalls, tree.outcome]).toEqual(["complete", 1, 2, "ok"]);
    expect(buildRunTree(run({ turns: [turn({ state: "failed" })] })).outcome).toBe("failed");
  });

  it("is empty for an unknown run", () => {
    const tree = buildRunTree(run({ run_id: "nope" }));
    expect([tree.empty, tree.roots, tree.outcome, tree.spanMs, tree.costUsd]).toEqual([true, [], "", null, null]);
  });
});

describe("helpers", () => {
  it("visibleIds skips the children of collapsed nodes", () => {
    const tree = buildRunTree(run({ edges: [row({ edge_uuid: "a" }), row({ edge_uuid: "b", parent_uuid: "a" }), row({ edge_uuid: "c" })] }));
    expect(visibleIds(tree.roots, () => true)).toEqual(["a", "b", "c"]);
    expect(visibleIds(tree.roots, (id) => id !== "a")).toEqual(["a", "c"]);
  });

  it("formats the span, the short id and a progress snapshot", () => {
    expect([spanLabel(null), spanLabel(850), spanLabel(42_000), spanLabel(192_000), spanLabel(3_840_000)]).toEqual(["—", "850ms", "42s", "3m12s", "1h04m"]);
    expect(shortRunId("7f3a9c1e2b4d5f60")).toBe("7f3a9c1e…");
    expect(shortRunId("board:42")).toBe("board:42");
    expect(progressLine({ text: " reading files " })).toBe("reading files");
    expect(progressLine({ tool_count: 3 })).toBe("3 tool calls");
    expect(progressLine(null)).toBe("");
  });
});

describe("outcomeLabel", () => {
  it("says one word for the ledger's ok and telemetry's completed", () => {
    expect(outcomeLabel("ok")).toBe("completed");
    expect(outcomeLabel("completed")).toBe("completed");
    expect(outcomeLabel("canceled")).toBe("cancelled");
    expect(outcomeLabel("failed")).toBe("failed");
  });
});
