import type { Archetype } from "../lib/types";

// The base SOUL an archetype seeds into the persona editor (ADR 0042). A bundle archetype
// may declare no inline `soul:` in its manifest (soul === ""), so picking it must not blank
// the editor — fall back to the base persona (the "basic" archetype's SOUL), leaving a
// sensible, editable starting point rather than an empty textarea.
export function personaSoul(a: Archetype, archetypes: Archetype[]): string {
  if (a.soul?.trim()) return a.soul;
  return archetypes.find((x) => x.id === "basic")?.soul ?? "";
}

// The Basic preset introduces itself as "I am protoAgent". When the operator names the
// agent something else in the wizard, the persona should agree with the name on screen —
// otherwise "scout" answers "I'm protoAgent" when asked who it is. Only that exact
// self-introduction is rewritten; a custom or edited SOUL is left alone.
export function namedSoul(soul: string, agentName: string): string {
  const name = agentName.trim();
  if (!name || name.toLowerCase() === "protoagent") return soul;
  return soul.replace(/\bI am protoAgent\b/, `I am ${name}`);
}
