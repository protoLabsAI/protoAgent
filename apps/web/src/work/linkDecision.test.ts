import { describe, expect, it } from "vitest";

import type { DirectiveRow } from "../lib/api/chat";
import type { DecisionRow } from "../lib/api/decisions";
import { linkDecision } from "./linkDecision";

// The directive is recorded at a fixed instant; each case places the answer relative to it.
const DIRECTIVE_AT = "2026-11-08T15:02:00+00:00";

function directive(patch: Partial<DirectiveRow> = {}): DirectiveRow {
  return {
    id: "dir-1", session_id: "here", quote: "Hold the release", conditions: "", scope: "",
    status: "in_force", created_at: DIRECTIVE_AT, closed_at: null, closed_by: null, close_reason: null, ...patch,
  };
}

function decision(id: string, patch: Partial<DecisionRow> = {}): DecisionRow {
  return {
    id, session_id: "here", agent: "protoEngineer", question: `Question ${id}`,
    options: [{ id: "go", label: "Go ahead" }], created_at: "2026-11-08T15:00:00+00:00",
    state: "answered", answer: "go", answered_at: "2026-11-08T15:01:00+00:00", expires_at: null,
    default_option: null, thread_id: `t-${id}`, interrupt_id: `i-${id}`, context: "", default_action: null,
    withdraw_reason: null, answered_by: "operator", answered_surface: "console",
    default_action_text: "carry on", answer_label: "Go ahead", ...patch,
  };
}

describe("linkDecision (ADR 0129 §3.1 interim heuristic)", () => {
  it("links an answer inside the window (answered 60s before the directive)", () => {
    const d = decision("inside", { answered_at: "2026-11-08T15:01:00+00:00" });
    expect(linkDecision(directive(), [d])).toBe(d);
  });

  it("links an answer exactly 120s before the directive (inclusive bound)", () => {
    const d = decision("edge", { answered_at: "2026-11-08T15:00:00+00:00" });
    expect(linkDecision(directive(), [d])).toBe(d);
  });

  it("does not link an answer 121s before the directive (just outside)", () => {
    const d = decision("stale", { answered_at: "2026-11-08T14:59:59+00:00" });
    expect(linkDecision(directive(), [d])).toBeNull();
  });

  it("does not link an answer recorded after the directive", () => {
    const d = decision("after", { answered_at: "2026-11-08T15:02:30+00:00" });
    expect(linkDecision(directive(), [d])).toBeNull();
  });

  it("does not link an answer from a different session", () => {
    const d = decision("elsewhere", { session_id: "other", answered_at: "2026-11-08T15:01:30+00:00" });
    expect(linkDecision(directive(), [d])).toBeNull();
  });

  it("takes the decision answered closest to the directive when several qualify", () => {
    const far = decision("far", { answered_at: "2026-11-08T15:00:30+00:00" }); // 90s before
    const near = decision("near", { answered_at: "2026-11-08T15:01:45+00:00" }); // 15s before
    expect(linkDecision(directive(), [far, near])).toBe(near);
    // Order-independent: the nearest still wins when it comes first.
    expect(linkDecision(directive(), [near, far])).toBe(near);
  });

  it("never links a decision with a missing or invalid answered_at", () => {
    expect(linkDecision(directive(), [decision("blank", { answered_at: null })])).toBeNull();
    expect(linkDecision(directive(), [decision("bad", { answered_at: "not-a-date" })])).toBeNull();
  });

  it("ignores decisions that are not answered, even within the window", () => {
    for (const state of ["pending", "expired", "cancelled", "withdrawn"] as const) {
      expect(linkDecision(directive(), [decision(state, { state })])).toBeNull();
    }
  });

  it("returns null when the directive timestamp is unparseable", () => {
    expect(linkDecision(directive({ created_at: "" }), [decision("ok")])).toBeNull();
  });
});
