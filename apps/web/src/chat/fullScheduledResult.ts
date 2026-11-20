// "View full result" on a scheduled result must show the run's WHOLE answer. The card's own
// `summary` is the server's 500-char cut (`_SCHEDULED_SUMMARY_CAP`), so the full text is read
// from where the run actually lives: the fire's durable turn in its Activity/job context, via
// the same `/turns` read the transcript uses. The summary is only the fallback.
import { api } from "../lib/api";

// The route's own ceiling — a fire's turn is looked up by task id inside its context, so
// read the widest tail the server allows (the shared `system:activity` context holds many).
const TURN_READ_LIMIT = 200;

/** The full text of the scheduled run `taskId` inside `activityContext`, or `fallback` when
 *  it cannot be read: either id missing, the read failing, or no such turn (swept, outside
 *  the newest tail, or still without an answer). Never throws — the caller always has
 *  something to show. */
export async function loadFullScheduledResult(
  activityContext: string | undefined,
  taskId: string | undefined,
  fallback: string,
): Promise<string> {
  const context = activityContext?.trim();
  const task = taskId?.trim();
  if (!context || !task) return fallback;
  try {
    const { turns } = await api.chatSessionTurns(context, TURN_READ_LIMIT);
    const text = turns?.find((turn) => turn.task_id === task)?.text;
    return typeof text === "string" && text.trim() ? text : fallback;
  } catch {
    return fallback;
  }
}
