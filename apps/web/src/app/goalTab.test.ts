import { describe, expect, it } from "vitest";

import { takeGoalKickoff } from "../chat/chat-store";
import { newGoalTab } from "./goalTab";

describe("newGoalTab", () => {
  it("targets a fresh chat session with kick:false, preserving the goal fields", () => {
    const { body } = newGoalTab({
      session_id: "operator",
      condition: "ship it",
      verifier: { type: "llm" },
      constraints: ["no new deps"],
    });
    expect(body.kick).toBe(false);
    expect(body.session_id).toMatch(/^chat-/); // a new tab, not the "operator" default
    expect(body.condition).toBe("ship it");
    expect(body.verifier).toEqual({ type: "llm" });
    expect(body.constraints).toEqual(["no new deps"]); // contract fields ride along
  });

  it("queues the kickoff only after onSet — so the drive turn can't race the goal POST", () => {
    const { body, onSet } = newGoalTab({ session_id: "x", condition: "do it", verifier: { type: "llm" } });
    // Nothing is queued for the session until the goal is actually set.
    expect(takeGoalKickoff(body.session_id)).toBeNull();
    onSet();
    const kickoff = takeGoalKickoff(body.session_id);
    expect(kickoff).toContain("do it");
    // takeGoalKickoff is idempotent — the slot fires the kickoff exactly once.
    expect(takeGoalKickoff(body.session_id)).toBeNull();
  });
});
