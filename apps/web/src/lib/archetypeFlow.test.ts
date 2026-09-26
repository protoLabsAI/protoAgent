import { describe, expect, it } from "vitest";

import { archetypeConfigFields, fieldId } from "./archetypeConfig";
import {
  archetypeFlowReducer,
  createAgentBody,
  initialArchetypeFlow,
  suggestedAgentName,
  type ArchetypeFlowAction,
  type ArchetypeFlowState,
} from "./archetypeFlow";
import type { Archetype, ArchetypePreview } from "./types";

// The two-step create-from-archetype flow (pick → set up), shared by the New-agent panel
// and the Setup Wizard. Pure reducer + payload builder — the transitions carry the real
// coverage: Back keeps every choice, a different pick resets answers, a typed name
// survives a re-pick, and Create sends name + answers + advanced values.

const ENGINEER: Archetype = {
  id: "engineer-archetype",
  label: "Engineer",
  icon: "wrench",
  blurb: "Ships code",
  bundle: "https://github.com/protoLabsAI/engineer-archetype",
  soul: "# Engineer",
  requires_tools: ["github_create_issue"],
};
const BASIC: Archetype = { id: "basic", label: "Basic", icon: "bot", blurb: "Plain", bundle: null, soul: "" };

const run = (actions: ArchetypeFlowAction[], from: ArchetypeFlowState = initialArchetypeFlow()) =>
  actions.reduce(archetypeFlowReducer, from);

const pickEngineer: ArchetypeFlowAction = { type: "pick", id: ENGINEER.id, suggestedName: "engineer", soul: ENGINEER.soul };

function engineerPreview(): ArchetypePreview {
  return {
    id: ENGINEER.id,
    bundle: {
      kind: "bundle",
      name: "Engineer",
      members: [],
      mcp: [
        {
          id: "github",
          name: "GitHub",
          template: {},
          inputs: [{ key: "github_token", label: "GitHub token", secret: true }],
        },
      ],
      secrets: [],
      config_inputs: [
        { key: "engineer.repo", label: "Start in a local repo", type: "path", help: "Registered as a project." },
        { key: "github.write", label: "Allow GitHub writes", type: "boolean", default: false, help: "Off = read-only." },
      ],
    },
  };
}

describe("suggestedAgentName", () => {
  it("slugs the archetype label", () => {
    expect(suggestedAgentName(ENGINEER)).toBe("engineer");
    expect(suggestedAgentName({ id: "pm", label: "Project Manager" })).toBe("project-manager");
    expect(suggestedAgentName({ id: "x", label: "  C++ / Rust!! " })).toBe("c-rust");
  });

  it("Basic, Custom and an unsluggable label suggest 'agent'", () => {
    expect(suggestedAgentName(BASIC)).toBe("agent");
    expect(suggestedAgentName({ id: "custom", label: "Custom" })).toBe("agent");
    expect(suggestedAgentName({ id: "z", label: "✨" })).toBe("agent");
    expect(suggestedAgentName(undefined)).toBe("agent");
  });

  it("steps around names already on the fleet (case-insensitive)", () => {
    expect(suggestedAgentName(ENGINEER, ["Engineer"])).toBe("engineer-2");
    expect(suggestedAgentName(ENGINEER, ["engineer", "engineer-2"])).toBe("engineer-3");
  });
});

describe("archetypeFlowReducer — step transitions", () => {
  it("starts on the picker with nothing typed", () => {
    const s = initialArchetypeFlow();
    expect(s.step).toBe("pick");
    expect(s.name).toBe("");
    expect(s.values).toEqual({});
  });

  it("pick pre-fills the suggested name + persona; next moves to set-up", () => {
    const s = run([pickEngineer, { type: "next" }]);
    expect(s.step).toBe("setup");
    expect(s.picked).toBe(ENGINEER.id);
    expect(s.name).toBe("engineer");
    expect(s.soul).toBe("# Engineer");
  });

  it("Back returns to the picker keeping the name, answers and persona edit", () => {
    const s = run([
      pickEngineer,
      { type: "next" },
      { type: "setName", name: "forge" },
      { type: "setValue", id: "config:engineer.repo", value: "/src/app" },
      { type: "setSoul", soul: "# Mine" },
      { type: "back" },
    ]);
    expect(s.step).toBe("pick");
    expect(s.name).toBe("forge");
    expect(s.values).toEqual({ "config:engineer.repo": "/src/app" });
    expect(s.soul).toBe("# Mine");
    // …and re-picking the SAME card then Next lands on set-up with all of it intact.
    const again = run([pickEngineer, { type: "next" }], s);
    expect(again.step).toBe("setup");
    expect(again.name).toBe("forge");
    expect(again.values).toEqual({ "config:engineer.repo": "/src/app" });
    expect(again.soul).toBe("# Mine");
  });

  it("picking a DIFFERENT card clears answers and re-seeds the persona, but keeps a typed name", () => {
    const s = run([
      pickEngineer,
      { type: "setName", name: "forge" },
      { type: "setValue", id: "input:GitHub:github_token", value: "ghp_x" },
      { type: "setSoul", soul: "# Mine" },
      { type: "pick", id: "basic", suggestedName: "agent", soul: "" },
    ]);
    expect(s.picked).toBe("basic");
    expect(s.values).toEqual({}); // a token typed for one archetype never carries into the next
    expect(s.name).toBe("forge");
    expect(s.soul).toBe("");
  });

  it("an untouched name follows the pick", () => {
    const s = run([pickEngineer, { type: "pick", id: "basic", suggestedName: "agent", soul: "" }]);
    expect(s.name).toBe("agent");
  });

  it("Next on the default card fills a still-empty name without a separate pick", () => {
    const s = run([{ type: "pick", id: "basic", suggestedName: "agent", soul: "" }, { type: "next" }]);
    expect(s.name).toBe("agent");
    expect(s.step).toBe("setup");
  });
});

describe("createAgentBody — the Create payload", () => {
  const fields = archetypeConfigFields(engineerPreview());
  const id = (key: string) => fieldId(fields.find((f) => f.key === key)!);

  it("carries the help line through to the form field", () => {
    expect(fields.find((f) => f.key === "engineer.repo")?.help).toBe("Registered as a project.");
    expect(fields.find((f) => f.key === "engineer.repo")?.kind).toBe("path");
  });

  it("includes name + bundle config answers + advanced MCP inputs + persona + contract", () => {
    const s = run([
      pickEngineer,
      { type: "next" },
      { type: "setName", name: "  forge " },
      { type: "setValue", id: id("engineer.repo"), value: "/src/app" },
      { type: "setValue", id: id("github.write"), value: "true" },
      { type: "setValue", id: id("github_token"), value: "ghp_x" },
      { type: "setSoul", soul: "# Mine" },
    ]);
    expect(createAgentBody(s, ENGINEER, fields)).toEqual({
      name: "forge",
      bundle: ENGINEER.bundle,
      soul: "# Mine",
      inputs: { github_token: "ghp_x" },
      secrets: undefined,
      config_inputs: { "engineer.repo": "/src/app", "github.write": true },
      requires_tools: ["github_create_issue"],
    });
  });

  it("skipped answers are omitted (env/default fallthrough); a blank persona falls back to the archetype's", () => {
    const s = run([pickEngineer, { type: "setSoul", soul: "  " }]);
    const body = createAgentBody(s, ENGINEER, fields);
    expect(body.inputs).toBeUndefined();
    expect(body.config_inputs).toBeUndefined();
    expect(body.soul).toBe("# Engineer");
  });

  it("Basic: no bundle, no soul, no contract", () => {
    const s = run([{ type: "pick", id: "basic", suggestedName: "agent", soul: "" }]);
    expect(createAgentBody(s, BASIC, [])).toEqual({
      name: "agent",
      bundle: null,
      soul: undefined,
      inputs: undefined,
      secrets: undefined,
      config_inputs: undefined,
      requires_tools: undefined,
    });
  });
});
