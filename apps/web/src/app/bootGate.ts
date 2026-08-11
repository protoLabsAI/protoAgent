// Boot-gate phase resolution (ADR 0042 §I). The full-screen boot gate shows one of several
// recovery states for the focused agent; this pure function picks WHICH, in precedence order,
// from the already-computed booleans off App's runtime probe. Extracted so the decision table
// is unit-tested without rendering App (the gate covers the whole app, so its logic is
// high-blast-radius — worth testing in isolation).

export type BootGatePhase =
  | "memberAuth" // a focused REMOTE member's stored token is wrong/missing (its probe 401s)
  | "unreachable" // a focused REMOTE member's box is offline / URL wrong (its probe 502s)
  | "notRunning" // a focused LOCAL peer didn't start (its probe 409s)
  | "failed" // the engine probe gave up (generic "isn't responding")
  | "stuck" // still loading past the grace period — offer "Continue anyway"
  | "loading"; // the normal cold-start wait

/** Whether the app shows at all (the gate hides). Ready when the engine is up, the
 *  setup wizard is due (no graph expected pre-setup), the operator escaped via
 *  "Continue anyway" — or the instance is deliberately SIGNED OUT of its native
 *  OAuth provider (#2513): `graph_auth_error` under `setup_complete` means the
 *  graph can't build until reconnect, which is a first-class state the shell
 *  banners, not a startup failure to wait out. */
export function bootGateReady(s: {
  bootOverride: boolean;
  setupPending: boolean;
  engineReady: boolean;
  signedOut: boolean;
}): boolean {
  return s.bootOverride || s.setupPending || s.engineReady || s.signedOut;
}

export function bootGatePhase(s: {
  memberAuthFailed: boolean;
  agentDown: boolean;
  /** agentDown caused by a 502 (remote unreachable) vs a 409 (local peer not running). */
  unreachable: boolean;
  bootFailed: boolean;
  bootStuck: boolean;
}): BootGatePhase {
  // Focused-agent faults win over the generic engine states: a member that's down/mis-tokened
  // needs its OWN recovery (return-to-host / update-token), not the hub's "isn't responding".
  if (s.memberAuthFailed) return "memberAuth";
  if (s.agentDown) return s.unreachable ? "unreachable" : "notRunning";
  if (s.bootFailed) return "failed";
  if (s.bootStuck) return "stuck";
  return "loading";
}
