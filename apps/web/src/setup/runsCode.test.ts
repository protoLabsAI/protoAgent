import { describe, expect, it } from "vitest";

import { agentWillRunCode } from "./runsCode";

const host = (cowork: boolean, exec: boolean) => [
  { id: "cowork", enabled: cowork },
  { id: "execute_code", enabled: exec },
];

describe("agentWillRunCode — the finish step only says the agent can run code when it can", () => {
  it.each([
    ["default host (Cowork on)", { hostPlugins: host(true, true) }, true],
    ["host disabled execute_code", { hostPlugins: host(true, false) }, false],
    ["host has Cowork off", { hostPlugins: host(false, false) }, false],
    ["archetype turns Cowork off", { hostPlugins: host(true, true), archetypeDisabled: ["cowork"] }, false],
    ["archetype turns execute_code off", { hostPlugins: host(true, true), archetypeDisabled: ["execute_code"] }, false],
    ["archetype turns Cowork on", { hostPlugins: host(false, false), archetypeEnabled: ["cowork"] }, true],
    ["archetype turns execute_code on by name", { hostPlugins: host(false, false), archetypeEnabled: ["execute_code"] }, true],
    ["host plugins not loaded yet", { hostPlugins: null }, true],
  ])("%s", (_name, opts, expected) => {
    expect(agentWillRunCode(opts)).toBe(expected);
  });
});
