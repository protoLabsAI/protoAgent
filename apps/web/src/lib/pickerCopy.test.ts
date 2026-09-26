import { describe, expect, it } from "vitest";

import {
  ADVANCED_CONNECTIONS_HELP,
  HARD_GATE_HINT,
  HARD_GATE_HINT_WIZARD,
  SETUP_OPTIONAL_HELP,
  SETUP_REQUIRED_HELP,
} from "./pickerCopy";

describe("archetype flow copy — one home for both entry points and the e2e specs", () => {
  it("the two entry points name their own terminal action", () => {
    expect(HARD_GATE_HINT).toContain("this agent can be created");
    expect(HARD_GATE_HINT_WIZARD).toContain("setup can continue");
  });

  it("the set-up step says 'leave blank to use this host's environment' once per group", () => {
    for (const copy of [SETUP_OPTIONAL_HELP, SETUP_REQUIRED_HELP, ADVANCED_CONNECTIONS_HELP]) {
      expect(copy).toMatch(/leave (the rest )?blank to use this host's environment/i);
    }
    expect(SETUP_REQUIRED_HELP).toContain("marked * are required");
  });
});
