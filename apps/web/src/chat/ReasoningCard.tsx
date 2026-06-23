import { Brain } from "lucide-react";

import { ToolCard } from "@protolabsai/ui/tool-card";

/**
 * A reasoning ("thinking") block rendered as a tool-style card: the SAME DS `ToolCard`
 * chrome as a tool call, COLLAPSED by default, so the model's native reasoning stacks
 * consistently with the tool cards in the thread instead of dominating it as big expanded
 * boxes. Expand to read the reasoning. A spinner shows while the model is still thinking;
 * the done-glyph is hidden (reasoning isn't a pass/fail result — see tool-calls.css).
 */
export function ReasoningCard({ text, streaming = false }: { text: string; streaming?: boolean }) {
  return (
    <ToolCard
      name="Reasoning"
      icon={<Brain size={13} />}
      status={streaming ? "running" : "done"}
      className="reasoning-card"
    >
      <div className="reasoning-text">{text}</div>
    </ToolCard>
  );
}
