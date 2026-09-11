import { Spinner } from "@protolabsai/ui/data";

import { useUI } from "../state/uiStore";
import { useRunningBackgroundJobs, type JobLite } from "./backgroundJobStore";

/** "delegate → sonnet: Land PR #13…" → "sonnet: Land PR #13…"; anything else as it is. */
export function jobLabel(job: Pick<JobLite, "description" | "subagent_type">): string {
  const d = (job.description || "").trim();
  const m = /^delegate\s*→\s*(.+)$/.exec(d);
  return m ? m[1] : d || job.subagent_type || "background job";
}

/** Background work still running for THIS chat, above the composer — so "is anything still
 *  going on here?" is answerable at a glance, not by opening a panel or scrolling back to
 *  find a row. Renders nothing when the chat has no running jobs. */
export function BackgroundWorkStrip({ sessionId }: { sessionId: string }) {
  const running = useRunningBackgroundJobs(sessionId);
  const openBackgroundJobs = useUI((s) => s.openBackgroundJobs);
  if (running.length === 0) return null;
  const labels = running.map(jobLabel);
  const count = running.length === 1 ? "1 background job running" : `${running.length} background jobs running`;
  return (
    <div className="chat-bgwork" role="status" aria-label={`${count}: ${labels.join("; ")}`} data-testid="chat-bgwork">
      <Spinner size={12} />
      <span className="chat-bgwork-list" title={labels.join("\n")}>
        <strong>{count}</strong> · {labels.join(" · ")}
      </span>
      <button type="button" className="chat-bgwork-open" onClick={openBackgroundJobs}>
        View
      </button>
    </div>
  );
}
