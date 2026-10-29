import type { SessionStatus } from "./chat-store";

// Regenerate is blocked — its button is disabled AND `regenerate()` bails BEFORE its
// destructive server rewind + local trim — while a turn is streaming or no model is
// connected. With no model the re-run can't start, so letting `regenerate()` proceed
// would discard the operator's last turn with nothing left to resend it (#4566). One
// predicate feeds both the disabled state and the guard, so the two can never drift.
export function regenerateBlocked(status: SessionStatus, noModel: boolean): boolean {
  return status === "streaming" || noModel;
}
