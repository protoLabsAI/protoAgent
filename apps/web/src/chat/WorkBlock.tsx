import { Cog } from "lucide-react";

import { ToolCard } from "@protolabsai/ui/tool-card";

import type { ChatPart, ToolCall } from "../lib/types";
import { toolsForGroup } from "./parts";
import { ReasoningCard } from "./ReasoningCard";
import { ToolCalls } from "./ToolCalls";

type ToolsPart = Extract<ChatPart, { kind: "tools" }>;

/**
 * Folds an agentic turn's intermediate reason→tool timeline behind ONE collapsed
 * disclosure ("Working… / Worked · N tools") so the final answer leads. WHILE STREAMING it
 * keeps the most-recent tool exposed below the summary (kept until a newer tool replaces
 * it) — so the block isn't fully collapsed while the agent works and you can see what it's
 * doing now. Expand the summary to replay the full timeline.
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
  const toolIds = new Set<string>();
  for (const p of parts) if (p.kind === "tools") for (const id of p.ids) toolIds.add(id);
  const n = toolIds.size;
  const label = streaming ? "Working…" : "Worked";
  const summary = n > 0 ? `${label} · ${n} ${n === 1 ? "tool" : "tools"}` : label;

  // While streaming, spotlight the most-recent tool below the summary.
  let spotlightIds: string[] = [];
  if (streaming) {
    const toolsParts = parts.filter((p): p is ToolsPart => p.kind === "tools");
    const last = toolsParts[toolsParts.length - 1];
    if (last && last.ids.length) spotlightIds = [last.ids[last.ids.length - 1]];
  }

  return (
    <div className="work">
      <ToolCard
        name={summary}
        icon={<Cog size={13} />}
        status={streaming ? "running" : "done"}
        className="work-block"
      >
        <div className="work-timeline">
          {parts.map((part, i) =>
            part.kind === "reasoning" ? (
              part.text.trim() ? <ReasoningCard key={i} text={part.text} /> : null
            ) : part.kind === "tools" ? (
              <ToolCalls key={i} calls={toolsForGroup(part.ids, toolCalls)} flat />
            ) : part.text.trim() ? (
              <div key={i} className="work-text">{part.text}</div>
            ) : null,
          )}
        </div>
      </ToolCard>
      {spotlightIds.length > 0 ? (
        <div className="work-spotlight">
          <ToolCalls calls={toolsForGroup(spotlightIds, toolCalls)} flat />
        </div>
      ) : null}
    </div>
  );
}
