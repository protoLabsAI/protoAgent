import { describe, expect, it } from "vitest";

import { nodeRuntimeView } from "./nodeRuntime";
import type { NodeRuntimePayload } from "../lib/types";

const base = (over: Partial<NodeRuntimePayload["node"]> = {}, install: Partial<NodeRuntimePayload["install"]> = {}): NodeRuntimePayload => ({
  node: {
    source: null,
    version: null,
    bin_dir: null,
    managed: false,
    managed_version: null,
    system: false,
    supported: true,
    target_version: "v24.18.0",
    ...over,
  },
  install: { state: "idle", pct: 0, message: "", error: null, ...install },
});

describe("nodeRuntimeView", () => {
  it("hides while data is loading", () => {
    expect(nodeRuntimeView(undefined)).toEqual({ kind: "hidden" });
  });

  it("hides when a system Node is available", () => {
    expect(nodeRuntimeView(base({ source: "system", version: "v22.0.0", system: true })).kind).toBe("hidden");
  });

  it("hides when a managed Node is available", () => {
    expect(nodeRuntimeView(base({ source: "managed", version: "v24.18.0", managed: true })).kind).toBe("hidden");
  });

  it("prompts to install when no Node and the platform is supported", () => {
    const v = nodeRuntimeView(base());
    expect(v).toEqual({ kind: "action", installing: false, pct: 0, message: "", error: null });
  });

  it("shows progress while installing (even before status flips)", () => {
    const v = nodeRuntimeView(base({}, { state: "running", pct: 42, message: "downloading… 42%" }));
    expect(v).toEqual({ kind: "action", installing: true, pct: 42, message: "downloading… 42%", error: null });
  });

  it("surfaces an install error on the action state", () => {
    const v = nodeRuntimeView(base({}, { state: "error", error: "integrity check failed" }));
    expect(v).toEqual({ kind: "action", installing: false, pct: 0, message: "", error: "integrity check failed" });
  });

  it("reports unsupported hosts", () => {
    expect(nodeRuntimeView(base({ supported: false })).kind).toBe("unsupported");
  });
});
