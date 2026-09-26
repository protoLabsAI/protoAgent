import { splitConfigValues, type ConfigField } from "./archetypeConfig";
import type { Archetype } from "./types";

// The two-step "create from an archetype" flow, shared by the fleet New-agent panel and
// the first-run Setup Wizard: (1) PICK an archetype — cards only, no name, no config;
// (2) SET UP — name first, then the bundle's questions, advanced options collapsed.
// Pure state + payload helpers (no React) so the transitions are unit-tested directly.

export type ArchetypeFlowStep = "pick" | "setup";

export type ArchetypeFlowState = {
  step: ArchetypeFlowStep;
  picked: string;
  name: string;
  // The operator typed their own name — a later re-pick must not overwrite it with the
  // next archetype's suggestion. Until then the name follows the picked archetype.
  nameTouched: boolean;
  // Collected field answers keyed by fieldId (see archetypeConfig).
  values: Record<string, string>;
  // The persona (SOUL.md) the agent is created with — seeded from the archetype on pick,
  // editable under Advanced. `soulTouched` protects an edit from a re-seed the same way.
  soul: string;
  soulTouched: boolean;
};

export type ArchetypeFlowAction =
  | { type: "pick"; id: string; suggestedName: string; soul: string }
  | { type: "next" }
  | { type: "back" }
  | { type: "setName"; name: string }
  | { type: "setValue"; id: string; value: string }
  | { type: "setSoul"; soul: string };

export function initialArchetypeFlow(picked = "basic"): ArchetypeFlowState {
  return { step: "pick", picked, name: "", nameTouched: false, values: {}, soul: "", soulTouched: false };
}

export function archetypeFlowReducer(state: ArchetypeFlowState, action: ArchetypeFlowAction): ArchetypeFlowState {
  switch (action.type) {
    case "pick": {
      // Re-picking the SAME card (e.g. after Back, or Next on the default card) keeps
      // every answer and only fills a name/persona that is still empty. A different card
      // clears the answers — a token typed for one archetype must not carry into the
      // next — and re-seeds the persona; a name the operator typed survives either way.
      if (action.id === state.picked) {
        return {
          ...state,
          name: state.nameTouched || state.name ? state.name : action.suggestedName,
          soul: state.soulTouched || state.soul ? state.soul : action.soul,
        };
      }
      return {
        ...state,
        picked: action.id,
        name: state.nameTouched ? state.name : action.suggestedName,
        values: {},
        soul: action.soul,
        soulTouched: false,
      };
    }
    case "next":
      return { ...state, step: "setup" };
    case "back":
      // Back returns to the picker with every choice intact.
      return { ...state, step: "pick" };
    case "setName":
      return { ...state, name: action.name, nameTouched: true };
    case "setValue":
      return { ...state, values: { ...state.values, [action.id]: action.value } };
    case "setSoul":
      return { ...state, soul: action.soul, soulTouched: true };
    default:
      return state;
  }
}

// A fleet agent's name is its id + URL slug.
export const AGENT_NAME_RE = /^[A-Za-z0-9-_]+$/;

// The name the setup step starts with: the archetype's label, slugged ("Project Manager"
// → "project-manager"), made unique against the names already taken (`-2`, `-3`, …).
// Basic/Custom (and a label that slugs to nothing) suggest "agent".
export function suggestedAgentName(archetype: Pick<Archetype, "id" | "label"> | undefined, taken: string[] = []): string {
  const generic = !archetype || archetype.id === "basic" || archetype.id === "custom";
  const slug = generic
    ? ""
    : archetype.label
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "");
  const base = slug || "agent";
  const used = new Set(taken.map((t) => t.toLowerCase()));
  if (!used.has(base)) return base;
  for (let n = 2; ; n++) {
    const candidate = `${base}-${n}`;
    if (!used.has(candidate)) return candidate;
  }
}

// The body POST /api/fleet takes — name, the archetype's bundle + contract, the persona,
// and every answer the operator gave (bundle config answers AND the advanced MCP inputs /
// declared secrets). Blank answers are dropped by splitConfigValues so the backend's
// env/default fallthrough still applies to whatever was skipped.
export function createAgentBody(
  state: Pick<ArchetypeFlowState, "name" | "values" | "soul">,
  archetype: Archetype | undefined,
  fields: ConfigField[],
) {
  const { inputs, secrets, config } = splitConfigValues(fields, state.values);
  const soul = state.soul.trim() ? state.soul : archetype?.soul || undefined;
  return {
    name: state.name.trim(),
    bundle: archetype?.bundle ?? null,
    soul: soul || undefined,
    inputs: Object.keys(inputs).length ? inputs : undefined,
    secrets: secrets.length ? secrets : undefined,
    config_inputs: Object.keys(config).length ? config : undefined,
    requires_tools: archetype?.requires_tools?.length ? archetype.requires_tools : undefined,
  };
}
