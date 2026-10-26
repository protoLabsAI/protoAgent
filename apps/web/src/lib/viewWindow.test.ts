import { describe, expect, it } from "vitest";

import { viewWindowKey } from "./viewWindow";

describe("viewWindowKey", () => {
  it("reads the plugin surface key off the view route", () => {
    expect(viewWindowKey("#/view/plugin:project_board:board")).toBe("plugin:project_board:board");
    expect(viewWindowKey("#/view/plugin:atlas:map/")).toBe("plugin:atlas:map");
  });

  it("is null for every normal console window", () => {
    for (const hash of ["", "#/", "#/agent/roxy-1a2b/", "#pair=abc", "#/view/chat", "#/view/plugin:x?y=1"]) {
      expect(viewWindowKey(hash)).toBeNull();
    }
  });
});
