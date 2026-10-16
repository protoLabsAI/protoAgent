import { Button } from "@protolabsai/ui/primitives";
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
 *  find a row. The `.chat-bgwork` container ALWAYS renders (one stable DOM node and live
 *  region): with running jobs it shows the spinner, labels and "View"; with none it renders
 *  empty with a `chat-bgwork--idle` modifier, keeping its reserved height so a job starting
 *  or the last one finishing never grows/shrinks the composer row and resizes the transcript
 *  under the reader (the slot is `grid: minmax(0,1fr) auto`). */
export function BackgroundWorkStrip({ sessionId }: { sessionId: string }) {
  const running = useRunningBackgroundJobs(sessionId);
  const openBackgroundJobs = useUI((s) => s.openBackgroundJobs);
  if (running.length === 0) {
    return <div className="chat-bgwork chat-bgwork--idle" role="status" data-testid="chat-bgwork" />;
  }
  const labels = running.map(jobLabel);
  const count = running.length === 1 ? "1 background job running" : `${running.length} background jobs running`;
  return (
    <div className="chat-bgwork" role="status" aria-label={`${count}: ${labels.join("; ")}`} data-testid="chat-bgwork">
      <Spinner size={12} />
      <span className="chat-bgwork-list" title={labels.join("\n")}>
        <strong className="chat-bgwork-count">{count}</strong> · {labels.join(" · ")}
      </span>
      <Button variant="ghost" size="sm" onClick={openBackgroundJobs}>
        View
      </Button>
    </div>
  );
}
