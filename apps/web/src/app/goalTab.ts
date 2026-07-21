import { chatStore, registerGoalKickoff } from "../chat/chat-store";
import type { GoalSetBody } from "../chat/goalForm";
import { useUI } from "../state/uiStore";

// Panel goal-create → drive the goal in a dedicated, focused chat tab (so the drive loop
// streams live into it, instead of running as a headless background turn). Both goal-create
// hosts (the Goals panel and the Work overview quick-add) route through here.
//
// Opens + focuses a new tab, labels it with the goal, and returns the `POST /api/goals` body
// (session_id = the new tab, `kick: false` — the tab fires the kickoff itself). The caller
// registers the kickoff via `onSet` AFTER the goal POST resolves, so the drive turn never
// races ahead of the goal being set on the server.
export function newGoalTab(body: GoalSetBody): { body: GoalSetBody; onSet: () => void } {
  const session = chatStore.createSession();
  chatStore.renameSession(session.id, body.condition);
  // On mobile the Work surface is pushed OVER chat; pop back to the chat root so the
  // driving tab is visible. On desktop chat is already alongside the panel — no-op.
  try {
    useUI.getState().setMobileActive("chat");
  } catch {
    // uiStore/mobile nav unavailable (tests) — the desktop path needs no surface switch.
  }
  return {
    body: { ...body, session_id: session.id, kick: false },
    onSet: () => registerGoalKickoff(session.id, `Start working toward the goal: ${body.condition}`),
  };
}
