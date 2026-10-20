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
// otherwise "scout" answers "I'm protoAgent" when asked who it is. Only an UNEDITED preset
// (soul === the archetype's seeded SOUL) is rewritten; once the operator has touched the
// text, it is saved exactly as written.
export function namedSoul(soul: string, agentName: string, seededSoul: string): string {
  const name = agentName.trim();
  if (!name || name.toLowerCase() === "protoagent" || soul !== seededSoul) return soul;
  // A replacer function, not a string: a name like "a$&b" must land literally.
  return soul.replace(/\bI am protoAgent\b/, () => `I am ${name}`);
}
