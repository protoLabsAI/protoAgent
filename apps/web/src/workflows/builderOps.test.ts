import { describe, expect, it } from "vitest";

import { type BuilderStep, downstreamOf, uniqueStepId, upstreamOf } from "./builderOps";

const step = (id: string, dependsOn: string[] = []): BuilderStep => ({
  id,
  subagent: "researcher",
  prompt: "p",
  dependsOn,
  gate: false,
});

describe("uniqueStepId", () => {
  it("appends -copy, then bumps a numeric suffix", () => {
    const steps = [step("gather"), step("gather-copy")];
    expect(uniqueStepId(steps, "gather")).toBe("gather-copy2");
    expect(uniqueStepId([step("x")], "x")).toBe("x-copy");
  });
});

describe("downstreamOf", () => {
  it("collects transitive dependents (the cycle guard for new edges)", () => {
    const dag = [step("a"), step("b", ["a"]), step("c", ["b"]), step("side")];
    expect([...downstreamOf(dag, "a")].sort()).toEqual(["b", "c"]);
    expect(downstreamOf(dag, "side").size).toBe(0);
  });
});

describe("upstreamOf", () => {
  it("collects transitive ancestors only (what a prompt can actually read)", () => {
    const dag = [step("a"), step("b", ["a"]), step("c", ["b"]), step("side")];
    expect([...upstreamOf(dag, "c")].sort()).toEqual(["a", "b"]);
    expect(upstreamOf(dag, "a").size).toBe(0);
    expect(upstreamOf(dag, "side").size).toBe(0);
  });
});
