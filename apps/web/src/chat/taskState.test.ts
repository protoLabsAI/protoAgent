// One task-state classification for every surface that settles a turn (#3957). The
// surfaces had drifted: TERMINAL included `rejected` while the failure check read
// `/fail|cancel/`, so a REJECTED turn settled as done.
import { describe, expect, it } from "vitest";

import { isTaskFailed, isTaskPaused, isTaskStateUnknown, isTaskTerminal } from "./taskState";

const SPELLINGS = {
  completed: ["completed", "TASK_STATE_COMPLETED"],
  failed: ["failed", "TASK_STATE_FAILED"],
  canceled: ["canceled", "cancelled", "TASK_STATE_CANCELED", "TASK_STATE_CANCELLED"],
  rejected: ["rejected", "TASK_STATE_REJECTED"],
  inputRequired: ["input-required", "TASK_STATE_INPUT_REQUIRED"],
  authRequired: ["auth-required", "TASK_STATE_AUTH_REQUIRED"],
  working: ["working", "submitted", "TASK_STATE_WORKING", "TASK_STATE_SUBMITTED"],
  unknown: ["unknown", "TASK_STATE_UNSPECIFIED"],
};

describe("task state classification (#3957)", () => {
  it("every terminal failure — failed, canceled AND rejected — is a failure", () => {
    for (const s of [...SPELLINGS.failed, ...SPELLINGS.canceled, ...SPELLINGS.rejected]) {
      expect(isTaskTerminal(s), s).toBe(true);
      expect(isTaskFailed(s), s).toBe(true);
    }
  });

  it("every failure is terminal, and completed is terminal but not a failure", () => {
    for (const s of SPELLINGS.completed) {
      expect(isTaskTerminal(s), s).toBe(true);
      expect(isTaskFailed(s), s).toBe(false);
    }
    for (const list of Object.values(SPELLINGS)) {
      for (const s of list) if (isTaskFailed(s)) expect(isTaskTerminal(s), s).toBe(true);
    }
  });

  it("input-required and auth-required are paused: not terminal, not failed", () => {
    for (const s of [...SPELLINGS.inputRequired, ...SPELLINGS.authRequired]) {
      expect(isTaskPaused(s), s).toBe(true);
      expect(isTaskTerminal(s), s).toBe(false);
      expect(isTaskFailed(s), s).toBe(false);
    }
  });

  it("unknown / UNSPECIFIED is its own class: not terminal, paused or failed", () => {
    for (const s of SPELLINGS.unknown) {
      expect(isTaskStateUnknown(s), s).toBe(true);
      expect(isTaskTerminal(s) || isTaskPaused(s) || isTaskFailed(s), s).toBe(false);
    }
    for (const s of [...SPELLINGS.working, ...SPELLINGS.completed, "", undefined]) {
      expect(isTaskStateUnknown(s), String(s)).toBe(false);
    }
  });

  it("working states are none of them", () => {
    for (const s of SPELLINGS.working) {
      expect(isTaskTerminal(s) || isTaskPaused(s) || isTaskFailed(s) || isTaskStateUnknown(s), s).toBe(false);
    }
  });
});
