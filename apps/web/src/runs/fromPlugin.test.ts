import { describe, expect, it } from "vitest";

import { parsePluginRunOpen } from "./fromPlugin";

// `protoagent:run:open` (#4726): the org chart's "View run" asks the console to open a run.
describe("parsePluginRunOpen", () => {
  it("reads the run id, trimmed", () => {
    expect(parsePluginRunOpen({ type: "protoagent:run:open", run_id: " 7f3a9c1e2b4d5f60 " })).toBe("7f3a9c1e2b4d5f60");
    expect(parsePluginRunOpen({ type: "protoagent:run:open", run_id: "board:42" })).toBe("board:42");
  });

  it("refuses anything that isn't a short, single-token id", () => {
    for (const run_id of ["", "  ", "a b", "x\u0000y", "x".repeat(201), 7, null, { id: "e1" }]) {
      expect(parsePluginRunOpen({ type: "protoagent:run:open", run_id }), String(run_id)).toBeNull();
    }
    expect(parsePluginRunOpen({ type: "protoagent:code:open", run_id: "e1" })).toBeNull();
    expect(parsePluginRunOpen(null)).toBeNull();
    expect(parsePluginRunOpen("protoagent:run:open")).toBeNull();
  });
});
