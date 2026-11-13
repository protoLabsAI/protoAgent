import { afterEach, describe, expect, it } from "vitest";

import { cacheAgentName, cachedAgentName } from "./agentNameCache";

afterEach(() => localStorage.clear());

describe("agentNameCache (the boot splash's name source)", () => {
  it("round-trips a name under the slug key the splash reads", () => {
    cacheAgentName("gizmo", "Gizmo Labs");
    expect(cachedAgentName("gizmo")).toBe("Gizmo Labs");
    // The exact raw key index.html's inline script reads before the storage seam loads.
    expect(localStorage.getItem("protoagent.agentName:gizmo")).toBe("Gizmo Labs");
  });

  it("is a no-op for a blank slug or name, and reads back null", () => {
    cacheAgentName("", "Nobody");
    cacheAgentName("ghost", "");
    expect(cachedAgentName("ghost")).toBeNull();
    expect(cachedAgentName("never-set")).toBeNull();
  });
});
