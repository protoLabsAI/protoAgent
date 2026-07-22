import { describe, it, expect, beforeEach } from "vitest";

import { markAgentOpened, readAgentRecency } from "./fleetPalette";

// The per-member palette entries (#1733) and the toggle picker (#1769) are folded into
// the Fleet Room — their helpers are gone. What's left is the recency store.

describe("agent recency store", () => {
  beforeEach(() => localStorage.clear());

  it("records and reads back last-opened timestamps", () => {
    markAgentOpened("ava", 100);
    markAgentOpened("bob", 200);
    expect(readAgentRecency()).toEqual({ ava: 100, bob: 200 });
  });

  it("returns an empty map when nothing is stored", () => {
    expect(readAgentRecency()).toEqual({});
  });
});
