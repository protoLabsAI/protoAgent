// How a `chat.resumed` push renders — extracted from ChatResumeWatch so the branching is
// unit-testable without mounting the component (the streamWatchdog precedent, #1982).
//
// The event is the operator's ONLY live view of a server-fired turn: scheduled fires, watch
// reactions and background push-resumes hold the connection open server-side and never
// stream to the browser, so whatever this builds is all the chat will ever show for them.
// That makes the failed case load-bearing — a crashed turn used to arrive carrying only its
// partial narration, rendering as an ordinary bubble that trailed off mid-sentence, while
// the reason sat unread in the task's terminal `status.message`.

export type ResumedTurnEvent = {
  session_id?: unknown;
  text?: unknown;
  task_id?: unknown;
  state?: unknown;
  error?: unknown;
};

export type ResumedTurnRender = {
  session: string;
  taskId: string;
  /** Dedup key for the ADR 0039 ring-buffer replay on reconnect. */
  key: string;
  failed: boolean;
  /** Markdown for the assistant bubble. */
  content: string;
  status: "done" | "error";
  toast: { tone: "info" | "error"; title: string; message: string };
  /** Title + body for the background/OS notification. */
  notify: { title: string; body: string };
};

/**
 * Build the render for a `chat.resumed` push, or null when there is nothing to show.
 *
 * Null only for a genuinely empty event — no session, or neither text nor error. A failure
 * qualifies on its error ALONE: a turn that died before saying anything is exactly the case
 * that used to disappear, and it is the one most worth seeing.
 */
export function resumedTurnRender(data: ResumedTurnEvent): ResumedTurnRender | null {
  const session = String(data.session_id ?? "");
  const text = String(data.text ?? "");
  const taskId = String(data.task_id ?? "");
  // `||`, not `??`: an EMPTY state must fall back too. `??` would leave "" in place, and
  // "" !== "completed" reads as failed — so a payload from a publisher that sets the key
  // but not a value would put a false "Turn failed" on a turn that went fine. A signal
  // that cries wolf is worse than no signal, and the server already resolves it the same
  // way (`str(... or "completed")`).
  const state = String(data.state || "completed");
  const error = String(data.error ?? "");
  const failed = state !== "completed";

  if (!session || (!text && !error)) return null;

  const content = failed && error ? (text ? `${text}\n\n---\n\n**Turn failed:** ${error}` : `**Turn failed:** ${error}`) : text;

  return {
    session,
    taskId,
    key: taskId || `${session}:${(text || error).slice(0, 32)}`,
    failed,
    content,
    // Matches ChatSurface's own `failed ? "error" : "done"` so a pushed failure and a
    // streamed one park the bubble identically.
    status: failed ? "error" : "done",
    toast: failed
      ? { tone: "error", title: "Task failed", message: error || "A server-fired turn ended without finishing." }
      : { tone: "info", title: "Task resumed", message: "A waited task picked back up in this chat." },
    notify: {
      title: failed ? "Task failed" : "Task resumed",
      body: (failed ? error || content : text).slice(0, 80),
    },
  };
}
