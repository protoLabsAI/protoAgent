import { describe, expect, it } from "vitest";

import { toolGroupHasSettings, toolGroupSettingKeys } from "./toolGroupSettings";

describe("toolGroupSettings", () => {
  it("Filesystem carries the fs toolset's gates, narrowest last", () => {
    // The category string is what the backend stamps on each tool (_tool_category in
    // console_handlers.py maps read_file/run_command/… → "Filesystem"), so this key must
    // track that name exactly or the group silently renders no settings.
    expect(toolGroupSettingKeys("Filesystem")).toEqual([
      "filesystem.enabled",
      "filesystem.allow_run",
      "filesystem.run_requires_approval",
      "filesystem.bypass_allowed",
    ]);
    expect(toolGroupHasSettings("Filesystem")).toBe(true);
  });

  it("a group with no settings gets no gear", () => {
    for (const cat of ["General", "Skills", "Memory", "Scheduler"]) {
      expect(toolGroupSettingKeys(cat)).toEqual([]);
      expect(toolGroupHasSettings(cat)).toBe(false);
    }
  });

  it("an unknown group (a plugin's, an MCP server's) is safe", () => {
    // Plugin/MCP group names are dynamic — never in the map, and must not throw.
    expect(toolGroupSettingKeys("some-plugin")).toEqual([]);
    expect(toolGroupHasSettings("")).toBe(false);
  });
});
