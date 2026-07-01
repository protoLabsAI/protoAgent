import type { Archetype } from "../lib/types";

// The base SOUL an archetype seeds into the persona editor (ADR 0042). A bundle archetype
// may declare no inline `soul:` in its manifest (soul === ""), so picking it must not blank
// the editor — fall back to the base persona (the "basic" archetype's SOUL), leaving a
// sensible, editable starting point rather than an empty textarea.
export function personaSoul(a: Archetype, archetypes: Archetype[]): string {
  if (a.soul?.trim()) return a.soul;
  return archetypes.find((x) => x.id === "basic")?.soul ?? "";
}
