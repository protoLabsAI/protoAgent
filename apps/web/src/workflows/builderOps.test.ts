import { describe, expect, it } from "vitest";

import { type BuilderStep, isLinearChain, reorderSteps, uniqueStepId } from "./builderOps";

const step = (id: string, dependsOn: string[] = []): BuilderStep => ({
  id,
  subagent: "researcher",
  prompt: "p",
  dependsOn,
  gate: false,
});

describe("reorderSteps", () => {
  it("re-threads a strict linear chain to the new visual order", () => {
    const chain = [step("a"), step("b", ["a"]), step("c", ["b"])];
    const next = reorderSteps(chain, 2, 0); // c to the front
    expect(next.map((s) => s.id)).toEqual(["c", "a", "b"]);
    expect(next.map((s) => s.dependsOn)).toEqual([[], ["c"], ["a"]]);
  });

  it("keeps depends_on untouched for a non-linear DAG (presentational reorder)", () => {
    const dag = [step("a"), step("b"), step("join", ["a", "b"])];
    const next = reorderSteps(dag, 1, 0);
    expect(next.map((s) => s.id)).toEqual(["b", "a", "join"]);
    expect(next.find((s) => s.id === "join")?.dependsOn).toEqual(["a", "b"]);
  });

  it("is a no-op on same-position or out-of-range moves", () => {
    const chain = [step("a"), step("b", ["a"])];
    expect(reorderSteps(chain, 1, 1)).toBe(chain);
    expect(reorderSteps(chain, 5, 0)).toBe(chain);
  });
});

describe("isLinearChain", () => {
  it("accepts first-no-deps then each-depends-on-predecessor", () => {
    expect(isLinearChain([step("a"), step("b", ["a"])])).toBe(true);
  });
  it("rejects parallel or multi-dep shapes", () => {
    expect(isLinearChain([step("a"), step("b")])).toBe(false);
    expect(isLinearChain([step("a"), step("b", ["a"]), step("c", ["a", "b"])])).toBe(false);
  });
});

describe("uniqueStepId", () => {
  it("appends -copy, then bumps a numeric suffix", () => {
    const steps = [step("gather"), step("gather-copy")];
    expect(uniqueStepId(steps, "gather")).toBe("gather-copy2");
    expect(uniqueStepId([step("x")], "x")).toBe("x-copy");
  });
});
