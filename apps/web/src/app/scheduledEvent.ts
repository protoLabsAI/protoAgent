import type { ChatMessage } from "../lib/types";

// Pure mapping from a `scheduler.completed` bus event to the display-only chat payload
// ScheduledWatch injects (#2990). Extracted from the watcher so it's unit-testable without
// mounting React / the toast + chat-store machinery. Type-only import → no runtime deps.

export type ParsedScheduled = {
  session: string;
  scheduled: NonNullable<ChatMessage["scheduled"]>;
};

/** Map the event, or return null when it lacks a job id or an origin session (r6 — a
 *  schedule created outside a chat delivers nothing). Coerces an unknown status to
 *  "completed" so a malformed payload never renders a broken card. */
export function parseScheduledEvent(data: Record<string, unknown>): ParsedScheduled | null {
  const jobId = String(data.job_id ?? "");
  const session = String(data.origin_session ?? "");
  if (!jobId || !session) return null;
  const statusRaw = String(data.status ?? "completed");
  const status = (["completed", "failed", "canceled"].includes(statusRaw) ? statusRaw : "completed") as
    | "completed"
    | "failed"
    | "canceled";
  return {
    session,
    scheduled: {
      jobId,
      firedAt: String(data.fired_at ?? ""),
      summary: String(data.summary ?? ""),
      status,
      collapse: Boolean(data.collapse),
      activityContext: String(data.activity_context ?? ""),
      taskId: String(data.task_id ?? ""),
    },
  };
}
