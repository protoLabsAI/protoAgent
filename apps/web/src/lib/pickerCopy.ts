// The archetype pickers' gate copy (#2977/#2979) — ONE home for the strings the
// New-agent panel and the Setup Wizard render and the e2e specs assert against, so a
// wording tweak can't silently desync a spec from its component. No React, no DOM:
// the specs under e2e/ import this directly.

// The hard gate: a required bundle `config_inputs` answer is blank. The panel creates
// an agent; the wizard finishes setup — same gate, each names its own terminal action.
export const HARD_GATE_HINT = "Fields marked * are needed before this agent can be created.";
export const HARD_GATE_HINT_WIZARD = "Fields marked * are needed before setup can finish.";

// The same hints while the Configure section is collapsed — the fields are hidden, so
// the hint also says where to go.
const collapsed = (hint: string) => `${hint.replace(/\.$/, "")} — open Configure.`;
export const HARD_GATE_HINT_COLLAPSED = collapsed(HARD_GATE_HINT);
export const HARD_GATE_HINT_WIZARD_COLLAPSED = collapsed(HARD_GATE_HINT_WIZARD);

// The soft hint: a required MCP input / declared secret left blank (skip → env fallback).
export const SOFT_GATE_HINT = "Fields marked * connect their server — fill them, or skip to use this host's environment.";

// The Configure toggle's trailing copy: required-answers vs optional-skip.
export const CONFIGURE_REQUIRED_COPY = "answers marked * are required";
export const CONFIGURE_OPTIONAL_COPY = "optional — skip to use this host's environment";
