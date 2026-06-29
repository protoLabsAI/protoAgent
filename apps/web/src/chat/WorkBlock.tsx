import { BookOpen, Brain, Wrench } from "lucide-react";

import { ToolCard } from "@protolabsai/ui/tool-card";
import { Tooltip } from "@protolabsai/ui/overlays";

import type { ChatPart, ToolCall } from "../lib/types";
import { ChatComponent } from "./ChatComponent";
import { toolsForGroup } from "./parts";
import { ReasoningCard } from "./ReasoningCard";
import { ToolCalls } from "./ToolCalls";

type ToolsPart = Extract<ChatPart, { kind: "tools" }>;

/** A skill load is a `load_skill` tool call; the skill name rides its JSON input. */
function skillName(input?: string): string {
  if (!input) return "skill";
  try {
    const parsed = JSON.parse(input) as { name?: unknown };
    return typeof parsed.name === "string" ? parsed.name : "skill";
  } catch {
    return "skill";
  }
}

function plural(n: number, one: string): string {
  return `${n} ${one}${n === 1 ? "" : "s"}`;
}

/**
 * Folds an agentic turn's intermediate reason→tool timeline behind ONE collapsed disclosure
 * so the final answer leads. The header tallies the WORK done this turn — reasoning steps,
 * tool calls, and skill loads — each as an icon + count, with a hover breakdown. WHILE
 * STREAMING it keeps the most-recent tool exposed below the summary (kept until a newer tool
 * replaces it) so the block isn't fully collapsed while the agent works. Expand to replay
 * the full timeline.
 */
export function WorkBlock({
  parts,
  toolCalls,
  streaming,
}: {
  parts: ChatPart[];
  toolCalls?: ToolCall[];
  streaming: boolean;
}) {
  // Tally the turn's work. Tool ids come from the timeline; resolve each to its call so we
  // can split plain tool calls from `load_skill` (skill loads get their own count).
  const callById = new Map((toolCalls ?? []).map((c) => [c.id, c]));
  const toolIds = new Set<string>();
  for (const p of parts) if (p.kind === "tools") for (const id of p.ids) toolIds.add(id);

  const toolTally = new Map<string, number>();
  const skillNames: string[] = [];
  for (const id of toolIds) {
    const call = callById.get(id);
    if (!call) continue;
    if (call.name === "load_skill") skillNames.push(skillName(call.input));
    else toolTally.set(call.name, (toolTally.get(call.name) ?? 0) + 1);
  }
  const toolCount = [...toolTally.values()].reduce((a, b) => a + b, 0);
  const skillCount = skillNames.length;
  const reasoningCount = parts.filter((p) => p.kind === "reasoning" && p.text.trim()).length;

  const label = streaming ? "Working…" : "Worked";
  const toolList = [...toolTally.entries()].map(([n, c]) => (c > 1 ? `${n} ×${c}` : n)).join(", ");

  // Hover breakdown — the lines behind the icon counts.
  const breakdown = (
    <div className="work-breakdown">
      {reasoningCount > 0 && <div>{plural(reasoningCount, "reasoning step")}</div>}
      {toolCount > 0 && (
        <div>
          {plural(toolCount, "tool call")}
          {toolList && <span className="work-breakdown-detail"> · {toolList}</span>}
        </div>
      )}
      {skillCount > 0 && (
        <div>
          {plural(skillCount, "skill load")}
          <span className="work-breakdown-detail"> · {skillNames.join(", ")}</span>
        </div>
      )}
    </div>
  );

  const header = (
    <Tooltip label={breakdown} side="top">
      <span className="work-stats">
        <span className="work-stat-label">{label}</span>
        {reasoningCount > 0 && (
          <span className="work-stat" aria-label={plural(reasoningCount, "reasoning step")}>
            <Brain size={12} />
            {reasoningCount}
          </span>
        )}
        {toolCount > 0 && (
          <span className="work-stat" aria-label={plural(toolCount, "tool call")}>
            <Wrench size={12} />
            {toolCount}
          </span>
        )}
        {skillCount > 0 && (
          <span className="work-stat" aria-label={plural(skillCount, "skill load")}>
            <BookOpen size={12} />
            {skillCount}
          </span>
        )}
      </span>
    </Tooltip>
  );

  // While streaming, spotlight ONLY the most-recent tool below the collapsed summary — the
  // reasoning, interstitial narration, and the streaming answer stay folded in the disclosure
  // (and the answer lands below the "Worked" summary once the turn settles). This is what keeps
  // a chatty/tool-heavy turn reading as one clean batch instead of interim text + split groups.
  let spotlightIds: string[] = [];
  if (streaming) {
    const toolsParts = parts.filter((p): p is ToolsPart => p.kind === "tools");
    const last = toolsParts[toolsParts.length - 1];
    if (last && last.ids.length) spotlightIds = [last.ids[last.ids.length - 1]];
  }

  return (
    <div className="work">
      <ToolCard name={header} status={streaming ? "running" : "done"} className="work-block">
        <div className="work-timeline">
          {parts.map((part, i) =>
            part.kind === "reasoning" ? (
              part.text.trim() ? <ReasoningCard key={i} text={part.text} /> : null
            ) : part.kind === "tools" ? (
              <ToolCalls key={i} calls={toolsForGroup(part.ids, toolCalls)} flat />
            ) : part.kind === "component" ? (
              <ChatComponent key={i} spec={part.spec} />
            ) : part.text.trim() ? (
              <div key={i} className="work-text">{part.text}</div>
            ) : null,
          )}
        </div>
      </ToolCard>
      {spotlightIds.length > 0 ? (
        <div className="work-spotlight">
          <ToolCalls calls={toolsForGroup(spotlightIds, toolCalls)} spotlight />
        </div>
      ) : null}
    </div>
  );
}
