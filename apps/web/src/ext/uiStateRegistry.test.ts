import { describe, expect, it } from "vitest";

import { createUISlice, registeredUISlices } from "./uiStateRegistry";

describe("UI-state slice registry (ADR 0061)", () => {
  it("creates a namespaced store; re-using a namespace returns the SAME instance + state", () => {
    const a = createUISlice("demo", { count: 0 });
    a.setState({ count: 7 });
    const b = createUISlice("demo", { count: 99 }); // same namespace → cached store, initial ignored
    expect(b).toBe(a);
    expect(b.getState().count).toBe(7);
  });

  it("supports the zustand store surface (getState / setState)", () => {
    const s = createUISlice("counter", { n: 1 });
    s.setState({ n: 5 });
    expect(s.getState().n).toBe(5);
  });

  it("distinct namespaces are independent and registered", () => {
    const alpha = createUISlice("alpha", { x: 1 });
    const beta = createUISlice("beta", { y: 2 });
    alpha.setState({ x: 10 });
    expect(alpha.getState().x).toBe(10);
    expect(beta.getState().y).toBe(2); // untouched
    expect(registeredUISlices()).toEqual(expect.arrayContaining(["demo", "counter", "alpha", "beta"]));
  });

  it("rejects an empty namespace", () => {
    expect(() => createUISlice("", { a: 1 })).toThrow();
  });
});
