import { describe, expect, it } from "vitest";

import { pythonRuntimeView } from "./pythonRuntime";
import type { PythonRuntimePayload } from "../lib/types";

const base = (
  over: Partial<PythonRuntimePayload["python"]> = {},
  install: Partial<PythonRuntimePayload["install"]> = {},
): PythonRuntimePayload => ({
  python: {
    needed: true,
    managed: false,
    managed_version: null,
    exe: null,
    baseline_installed: false,
    baseline_current: false,
    supported: true,
    target_version: "3.12.13",
    ...over,
  },
  install: { state: "idle", pct: 0, message: "", error: null, ...install },
});

describe("pythonRuntimeView", () => {
  it("hides while data is loading", () => {
    expect(pythonRuntimeView(undefined)).toEqual({ kind: "hidden" });
  });

  it("hides on source runs — the backend spawns its own interpreter there", () => {
    expect(pythonRuntimeView(base({ needed: false })).kind).toBe("hidden");
  });

  it("hides once the runtime and its baseline are in place", () => {
    expect(
      pythonRuntimeView(base({ managed: true, baseline_installed: true, baseline_current: true })).kind,
    ).toBe("hidden");
  });

  it("prompts to install when the desktop build has no runtime", () => {
    const v = pythonRuntimeView(base());
    expect(v).toEqual({ kind: "action", installing: false, pct: 0, message: "", error: null, stale: false });
  });

  it("offers a refresh when the runtime is present but the doc baseline is stale", () => {
    const v = pythonRuntimeView(base({ managed: true, baseline_installed: true, baseline_current: false }));
    expect(v).toEqual({ kind: "action", installing: false, pct: 0, message: "", error: null, stale: true });
  });

  it("shows progress while installing (even before status flips)", () => {
    const v = pythonRuntimeView(base({}, { state: "running", pct: 42, message: "downloading… 42%" }));
    expect(v).toEqual({
      kind: "action",
      installing: true,
      pct: 42,
      message: "downloading… 42%",
      error: null,
      stale: false,
    });
  });

  it("surfaces an install error on the action state", () => {
    const v = pythonRuntimeView(base({}, { state: "error", error: "integrity check failed" }));
    expect(v).toEqual({
      kind: "action",
      installing: false,
      pct: 0,
      message: "",
      error: "integrity check failed",
      stale: false,
    });
  });

  it("reports unsupported hosts (only when the runtime would actually be used)", () => {
    expect(pythonRuntimeView(base({ supported: false })).kind).toBe("unsupported");
    expect(pythonRuntimeView(base({ supported: false, needed: false })).kind).toBe("hidden");
  });
});
