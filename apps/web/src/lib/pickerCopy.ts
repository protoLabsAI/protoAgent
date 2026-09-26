// The archetype flow's copy (#2977/#2979, two-step set-up) — ONE home for the strings
// the New-agent panel and the Setup Wizard render and the e2e specs assert against, so a
// wording tweak can't silently desync a spec from its component. No React, no DOM: the
// specs under e2e/ import this directly.

// The hard gate: a required bundle `config_inputs` answer is blank. The panel creates
// an agent; the wizard finishes setup — same gate, each names its own terminal action.
export const HARD_GATE_HINT = "Fields marked * are needed before this agent can be created.";
export const HARD_GATE_HINT_WIZARD = "Fields marked * are needed before setup can continue.";

// The soft hint: a required MCP input / declared secret left blank (skip → env fallback).
export const SOFT_GATE_HINT = "Fields marked * connect their server — fill them, or skip to use this host's environment.";
// …while the Advanced section holding those fields is collapsed.
export const SOFT_GATE_HINT_COLLAPSED = "Some connections under Advanced need a value — fill them, or skip to use this host's environment.";

// The set-up step says "optional" ONCE, above the bundle's questions — not per field.
export const SETUP_OPTIONAL_HELP = "Optional — leave blank to use this host's environment.";
export const SETUP_REQUIRED_HELP = "Fields marked * are required. Leave the rest blank to use this host's environment.";
// Above the MCP-server inputs + declared secrets under Advanced.
export const ADVANCED_CONNECTIONS_HELP = "Connections — leave blank to use this host's environment.";
