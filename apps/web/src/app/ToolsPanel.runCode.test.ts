import { afterEach, describe, expect, it, vi } from "vitest";

import { acknowledgeRunCode, shouldConfirmRunCode } from "./ToolsPanel";

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("the run-code reminder in Tools", () => {
  it("stops only for turning execute_code on, and only until it has been acknowledged", () => {
    expect(shouldConfirmRunCode("execute_code", true)).toBe(true);
    expect(shouldConfirmRunCode("execute_code", false)).toBe(false); // turning it off never asks
    expect(shouldConfirmRunCode("run_command", true)).toBe(false);
    acknowledgeRunCode();
    expect(shouldConfirmRunCode("execute_code", true)).toBe(false);
  });

  it("shows again when storage is unavailable, never skips", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(shouldConfirmRunCode("execute_code", true)).toBe(true);
  });
});
