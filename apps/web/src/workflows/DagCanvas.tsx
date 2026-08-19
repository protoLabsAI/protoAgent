import "@xyflow/react/dist/style.css";

import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import { Pause } from "lucide-react";
import { useMemo } from "react";

import { Badge } from "@protolabsai/ui/primitives";

import { type BuilderStep, downstreamOf } from "./builderOps";
import { computeLanes } from "./RunTimeline";

// The builder's node-and-edge DAG surface (n8n/ComfyUI shape, operator's call):
// nodes are steps, edges are `depends_on` (source → target = target runs after
// source). Left-to-right flow, lane-derived auto-layout for steps without a
// saved position; dragged positions persist on the step's unmanaged `ui` key
// (the extras-preservation contract carries it through save untouched).
// Connecting is cycle-guarded here with the same downstreamOf() the editor's
// "after:" pills use — the server's validator stays the final word.

export type StepFlag = { error: boolean };

const LANE_X = 240;
const LANE_Y = 96;

function StepNode({ data, selected }: NodeProps) {
  const d = data as { step: BuilderStep; flag: StepFlag };
  return (
    <div className={`dag-node ${selected ? "dag-node-sel" : ""}`} data-step-id={d.step.id}>
      <Handle type="target" position={Position.Left} className="dag-handle" />
      <span className="dag-node-head">
        <span className={`builder-dot ${d.flag.error ? "builder-dot-err" : "builder-dot-ok"}`} />
        <strong>{d.step.id.trim() || "(unnamed)"}</strong>
        {d.step.gate ? <Pause size={11} aria-label="operator gate" /> : null}
      </span>
      <span className="dag-node-sub">
        <Badge>{d.step.subagent}</Badge>
      </span>
      <Handle type="source" position={Position.Right} className="dag-handle" />
    </div>
  );
}

const NODE_TYPES = { step: StepNode };

/** Lane-derived default position for steps that were never dragged. */
export function autoPosition(steps: BuilderStep[]): Map<string, { x: number; y: number }> {
  const lanes = computeLanes(
    steps
      .filter((s) => s.id.trim())
      .map((s) => ({ id: s.id.trim(), subagent: s.subagent, depends_on: s.dependsOn, gate: s.gate ? "human" : undefined })),
  );
  const out = new Map<string, { x: number; y: number }>();
  lanes.forEach((lane, x) => lane.forEach((id, y) => out.set(id, { x: x * LANE_X, y: y * LANE_Y })));
  return out;
}

export function DagCanvas({
  steps,
  focusIndex,
  stepFlags,
  onSelect,
  onConnect,
  onDisconnect,
  onMove,
  onCycleRefused,
}: {
  steps: BuilderStep[];
  focusIndex: number | null;
  stepFlags: StepFlag[];
  onSelect: (index: number | null) => void;
  onConnect: (sourceId: string, targetId: string) => void;
  onDisconnect: (sourceId: string, targetId: string) => void;
  onMove: (index: number, pos: { x: number; y: number }) => void;
  onCycleRefused: () => void;
}) {
  const auto = useMemo(() => autoPosition(steps), [steps]);

  const nodes: Node[] = steps.map((step, i) => ({
    id: step.id.trim() || `__idx${i}`,
    type: "step",
    position: step.ui ?? auto.get(step.id.trim()) ?? { x: 0, y: i * LANE_Y },
    selected: focusIndex === i,
    data: { step, flag: stepFlags[i] ?? { error: false } },
  }));

  const edges: Edge[] = steps.flatMap((step) =>
    step.dependsOn
      .filter((dep) => steps.some((s) => s.id.trim() === dep))
      .map((dep) => ({
        id: `${dep}->${step.id.trim()}`,
        source: dep,
        target: step.id.trim(),
        deletable: true,
      })),
  );

  const byId = new Map(steps.map((s, i) => [s.id.trim(), i]));

  const handleConnect = (c: Connection) => {
    if (!c.source || !c.target || c.source === c.target) return;
    // target would depend on source; refuse when source already runs after target
    if (downstreamOf(steps, c.target).has(c.source)) {
      onCycleRefused();
      return;
    }
    onConnect(c.source, c.target);
  };

  const handleNodeChanges = (changes: NodeChange[]) => {
    for (const ch of changes) {
      if (ch.type === "position" && ch.position && !ch.dragging) {
        const i = byId.get(ch.id);
        if (i != null) onMove(i, { x: Math.round(ch.position.x), y: Math.round(ch.position.y) });
      }
      if (ch.type === "select" && ch.selected) {
        const i = byId.get(ch.id);
        if (i != null) onSelect(i);
      }
    }
  };

  const handleEdgeChanges = (changes: EdgeChange[]) => {
    for (const ch of changes) {
      if (ch.type === "remove") {
        const [source, target] = String(ch.id).split("->");
        if (source && target) onDisconnect(source, target);
      }
    }
  };

  return (
    <div className="dag-canvas" data-testid="dag-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onConnect={handleConnect}
        onNodesChange={handleNodeChanges}
        onEdgesChange={handleEdgeChanges}
        onPaneClick={() => onSelect(null)}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        proOptions={{ hideAttribution: true }}
        deleteKeyCode={["Backspace", "Delete"]}
        nodesConnectable
        elementsSelectable
      >
        <Background gap={18} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
