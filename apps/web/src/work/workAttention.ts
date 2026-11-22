/** Action eligibility shared by the list, detail and shell badge. */
import type { DecisionRow } from "../lib/api/decisions";
import type { GoalState, Task } from "../lib/types";
import type { GroupableWorkItem } from "./tone";

export function hasResolvingAction(item: GroupableWorkItem): boolean {
  if (item.actions) return item.actions.some((action) => action.resolves === true && action.id !== "dismiss");
  const raw = item.raw as Record<string, unknown> | null;
  if (!raw) return false;
  switch (item.kind) {
    case "decision": return (raw as DecisionRow).options?.length > 0 || !!raw.session_id;
    case "goal": return !!raw.session_id && ["exhausted", "unachievable"].includes(String(raw.status));
    // WaitingTaskDetail can send a reply even when the original chat no longer exists.
    case "task": return !!item.id && raw.status === "waiting";
    default: return false;
  }
}

export function workCause(item: GroupableWorkItem): string {
  if (item.cause) return item.cause;
  if (item.kind === "task") {
    const task = item.raw as Task;
    return task?.ask?.trim() || task?.description?.trim() || "";
  }
  if (item.kind === "goal") {
    const goal = item.raw as GoalState;
    return goal?.abandon_reason || goal?.last_reason || "";
  }
  return "";
}

export function workLead(item: GroupableWorkItem): string {
  if (item.kind === "task") return "Your reply is needed before work can continue.";
  if (item.kind === "goal") return "Work stopped before the goal was reached.";
  if (item.kind === "delivery") return "A message was not delivered.";
  return item.title;
}
