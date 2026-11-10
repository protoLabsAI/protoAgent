import { create } from "zustand";
import { ToolCard, ToolCardList, ToolSection } from "@protolabsai/ui/tool-card";
import { applyProgress, type ProgressTool } from "./background-jobs";

// The shell widget collects progress even while Work is closed. Share that history
// with both readers so opening a job doesn't discard its earlier tool outputs.
export const useBackgroundProgress = create<{ jobs: Record<string, ProgressTool[]> }>(() => ({ jobs: {} }));

export function recordBackgroundProgress(id: string, frame: Parameters<typeof applyProgress>[1]) {
  useBackgroundProgress.setState(({ jobs }) => ({
    jobs: { ...jobs, [id]: applyProgress(jobs[id] || [], frame) },
  }));
}

export function BackgroundToolFeed({ tools }: { tools: ProgressTool[] }) {
  if (!tools.length) return null;
  return (
    <ToolCardList className="bg-jobs-feed">
      {tools.map((tool) => (
        <ToolCard key={tool.id} name={tool.tool} status={tool.error ? "error" : tool.done ? "done" : "running"}>
          {tool.output ? <ToolSection label="output">{tool.output}</ToolSection> : null}
        </ToolCard>
      ))}
    </ToolCardList>
  );
}
