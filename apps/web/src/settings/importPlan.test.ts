import { describe, expect, it } from "vitest";

import { importButtonLabel, neededSecrets, planSummary } from "./importPlan";
import type { SnapshotImportPlan } from "../lib/types";

const plan = (over: Partial<SnapshotImportPlan> = {}): SnapshotImportPlan => ({
  mode: "plan",
  agent_name: "vera",
  plugins: [],
  required_secrets: [],
  capabilities: [],
  has_soul: true,
  skill_files: 0,
  mcp_servers: [],
  notes: [],
  runs_code: false,
  ...over,
});

const secret = (name: string, was_set: boolean) => ({ name, kind: "config", description: "", was_set });

describe("neededSecrets", () => {
  it("asks only for credentials the SOURCE agent actually had", () => {
    // A merely-declared credential would put an empty field in front of the operator
    // implying the import is incomplete, when it is in exactly the state the original was.
    const p = plan({ required_secrets: [secret("model.api_key", true), secret("discord.bot_token", false)] });
    expect(neededSecrets(p).map((s) => s.name)).toEqual(["model.api_key"]);
  });

  it("is empty for no plan", () => {
    expect(neededSecrets(null)).toEqual([]);
  });
});

describe("importButtonLabel", () => {
  it("names the code execution ON the button that performs it", () => {
    // Consent has to live on the control, not only in a paragraph that can be scrolled past.
    expect(importButtonLabel(plan({ plugins: [{ id: "a", url: "u", ref: "r", recognized: true }] }))).toBe(
      "Install 1 plugin and create agent",
    );
  });

  it("pluralizes", () => {
    const two = [
      { id: "a", url: "u", ref: "r", recognized: true },
      { id: "b", url: "u2", ref: "r2", recognized: false },
    ];
    expect(importButtonLabel(plan({ plugins: two }))).toBe("Install 2 plugins and create agent");
  });

  it("stays plain when there is no code to run", () => {
    expect(importButtonLabel(plan())).toBe("Create agent");
  });
});

describe("planSummary", () => {
  it("says a snapshot yields a FRESH agent", () => {
    // The one thing an operator is most likely to assume wrongly (ADR 0091 D4).
    expect(planSummary(plan())).toContain("FRESH agent");
    expect(planSummary(plan())).toContain("no conversation history");
  });

  it("counts what is present and singularizes correctly", () => {
    const s = planSummary(plan({ skill_files: 1, mcp_servers: ["a"], plugins: [{ id: "x", url: "u", ref: "", recognized: true }] }));
    expect(s).toContain("a persona");
    expect(s).toContain("1 plugin,");
    expect(s).toContain("1 skill file");
    expect(s).toContain("1 MCP server");
  });

  it("reports an absent persona rather than staying silent", () => {
    expect(planSummary(plan({ has_soul: false }))).toContain("no persona");
  });
});
