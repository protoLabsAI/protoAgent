import { Cog } from "lucide-react";

import { ToolCard } from "@protolabsai/ui/tool-card";

import type { ChatPart, ToolCall } from "../lib/types";
import { toolsForGroup } from "./parts";
import { ReasoningCard } from "./ReasoningCard";
import { ToolCalls } from "./ToolCalls";

/**
 * Folds an agentic turn's whole intermediate timeline — every reasoning step and tool
 * call — behind ONE collapsed disclosure ("Working… / Worked · N tools"), so the final
 * answer leads the message and the reason→tool→reason scratchpad doesn't dominate the
 * thread. Expand to replay the full timeline (the collapsed reasoning + tool cards in
 * emission order; `flat` so there are no nested fold chips inside).
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

  return (
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
            // A rare pre-answer narration run that landed before the final answer.
            <div key={i} className="work-text">{part.text}</div>
          ) : null,
        )}
      </div>
    </ToolCard>
  );
}
