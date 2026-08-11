import { describe, it, expect } from "vitest";

import { bootGatePhase, bootGateReady } from "./bootGate";

const NONE = {
  memberAuthFailed: false,
  agentDown: false,
  unreachable: false,
  bootFailed: false,
  bootStuck: false,
};

describe("bootGatePhase — focused-agent recovery precedence (ADR 0042 §I)", () => {
  it("no faults → the normal cold-start wait", () => {
    expect(bootGatePhase(NONE)).toBe("loading");
  });

  it("a member's bad token (401) wins over everything", () => {
    // memberAuthFailed must beat agentDown/failed/stuck so the operator gets the
    // "update its token" recovery, not a generic "isn't responding".
    expect(bootGatePhase({ ...NONE, memberAuthFailed: true })).toBe("memberAuth");
    expect(
      bootGatePhase({ memberAuthFailed: true, agentDown: true, unreachable: true, bootFailed: true, bootStuck: true }),
    ).toBe("memberAuth");
  });

  it("agentDown splits on reachability: 502 → unreachable, 409 → notRunning", () => {
    expect(bootGatePhase({ ...NONE, agentDown: true, unreachable: true })).toBe("unreachable");
    expect(bootGatePhase({ ...NONE, agentDown: true, unreachable: false })).toBe("notRunning");
  });

  it("agentDown beats the generic engine states", () => {
    expect(bootGatePhase({ ...NONE, agentDown: true, unreachable: true, bootFailed: true, bootStuck: true })).toBe(
      "unreachable",
    );
    expect(bootGatePhase({ ...NONE, agentDown: true, bootFailed: true, bootStuck: true })).toBe("notRunning");
  });

  it("failed beats stuck; stuck beats loading", () => {
    expect(bootGatePhase({ ...NONE, bootFailed: true, bootStuck: true })).toBe("failed");
    expect(bootGatePhase({ ...NONE, bootStuck: true })).toBe("stuck");
  });

  it("unreachable is ignored unless agentDown is set (a stray 502 mid-boot doesn't jump the gun)", () => {
    // agentDown already encodes the failureCount>=6 guard; `unreachable` alone must not
    // short-circuit the normal loading state.
    expect(bootGatePhase({ ...NONE, unreachable: true })).toBe("loading");
  });
});

describe("bootGateReady — when the shell shows instead of the gate", () => {
  const DOWN = { bootOverride: false, setupPending: false, engineReady: false, signedOut: false };

  it("nothing resolved → gate stays up (cold start)", () => {
    expect(bootGateReady(DOWN)).toBe(false);
  });

  it("each ready condition independently shows the shell", () => {
    expect(bootGateReady({ ...DOWN, engineReady: true })).toBe(true);
    expect(bootGateReady({ ...DOWN, setupPending: true })).toBe(true);
    expect(bootGateReady({ ...DOWN, bootOverride: true })).toBe(true);
  });

  it("a deliberate OAuth signed-out state is READY, not a failed boot (#2513)", () => {
    // graph_auth_error under setup_complete used to fall through every ready
    // condition, so the operator waited out the 45s "stuck" gate and read an
    // intentional disconnect as a broken startup.
    expect(bootGateReady({ ...DOWN, signedOut: true })).toBe(true);
  });
});
