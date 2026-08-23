import { describe, expect, it } from "vitest";

import {
  HARD_GATE_HINT,
  HARD_GATE_HINT_COLLAPSED,
  HARD_GATE_HINT_WIZARD,
  HARD_GATE_HINT_WIZARD_COLLAPSED,
} from "./pickerCopy";

describe("picker gate copy — one home for both pickers and the e2e specs", () => {
  it("the collapsed variants extend the open hint with where to go", () => {
    expect(HARD_GATE_HINT_COLLAPSED).toBe("Fields marked * are needed before this agent can be created — open Configure.");
    expect(HARD_GATE_HINT_WIZARD_COLLAPSED).toBe("Fields marked * are needed before setup can finish — open Configure.");
    expect(HARD_GATE_HINT_COLLAPSED.startsWith(HARD_GATE_HINT.replace(/\.$/, ""))).toBe(true);
    expect(HARD_GATE_HINT_WIZARD_COLLAPSED.startsWith(HARD_GATE_HINT_WIZARD.replace(/\.$/, ""))).toBe(true);
  });

  it("the two pickers name their own terminal action", () => {
    expect(HARD_GATE_HINT).toContain("this agent can be created");
    expect(HARD_GATE_HINT_WIZARD).toContain("setup can finish");
  });
});
