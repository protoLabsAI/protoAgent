// @vitest-environment node
import { describe, expect, it } from "vitest";

import type { SettingsField } from "../lib/types";
import { fieldOrigin } from "./SettingsCategory";

// #5812: a setting says where its value comes from only when that tells you something.
function field(over: Partial<SettingsField>): SettingsField {
  return { key: "k", label: "K", type: "string", section: "S", restart: false, options: [], scope: "agent", source: "default", value: "", default: "", ...over };
}

describe("fieldOrigin on a fleet member", () => {
  it("names the machine when the value comes from it", () => {
    expect(fieldOrigin(field({ scope: "host", source: "host", value: "Josh" }), false)?.label).toBe("From this machine");
  });

  it("says what the machine uses when this agent sets its own, and offers the reset", () => {
    const o = fieldOrigin(field({ scope: "host", source: "agent", value: "a", host_value: "protolabs/reasoning" }), false);
    expect(o).toMatchObject({ label: "Set for this agent", overridden: true, note: "The machine uses protolabs/reasoning." });
    expect(fieldOrigin(field({ scope: "host", source: "agent", value: "a" }), false)?.note).toBe("The machine doesn't set this.");
  });

  it("marks a real built-in value and stays quiet on an empty one", () => {
    expect(fieldOrigin(field({ type: "number", value: 0.2, default: 0.2 }), false)?.label).toBe("Built-in");
    expect(fieldOrigin(field({ value: "" }), false)).toBeNull();
    expect(fieldOrigin(field({ type: "string_list", value: [] }), false)).toBeNull();
    expect(fieldOrigin(field({ type: "bool", value: false }), false)).toBeNull();
    expect(fieldOrigin(field({ scope: "host", source: "host", value: "" }), false)).toBeNull();
  });

  it("says nothing for an agent's own per-agent setting", () => {
    expect(fieldOrigin(field({ source: "agent", value: "x" }), false)).toBeNull();
  });
});

describe("fieldOrigin on the main instance", () => {
  it("says a set box-wide value reaches every agent, and who sets their own", () => {
    const o = fieldOrigin(field({ scope: "host", source: "host", value: "Josh", set_by_agents: ["Frank"] }), true);
    expect(o).toMatchObject({ label: "Every agent on this machine", note: "1 agent sets its own: Frank." });
    const many = fieldOrigin(field({ scope: "host", source: "host", value: "m", set_by_agents: ["A", "B", "C", "D", "E"] }), true);
    expect(many?.note).toBe("5 agents set their own: A, B, C and 2 more.");
  });

  it("stays quiet on an empty box-wide value", () => {
    expect(fieldOrigin(field({ scope: "host", source: "default", value: "" }), true)).toBeNull();
  });

  it("warns when the main instance's own config shadows the box value", () => {
    expect(fieldOrigin(field({ scope: "host", source: "agent", value: 0.4 }), true)).toMatchObject({
      label: "Set for this agent only",
      status: "warning",
      overridden: true,
    });
  });

  it("marks built-in values on the main instance too", () => {
    expect(fieldOrigin(field({ type: "number", value: 0.2 }), true)?.label).toBe("Built-in");
  });
});
