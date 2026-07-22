// Pure half of the settings flag gate (ADR 0068): drop sections whose `flag` resolves
// off. Extracted from SettingsSurface so the gating is unit-testable without importing
// the whole settings tree (secretsGate.test.ts exercises it with the flag both ways) —
// the component wires `flagOn` to useFlagPredicate().
export type GatedSection = { id: string; flag?: string };

export function visibleSections<T extends GatedSection>(list: T[], flagOn: (id: string) => boolean): T[] {
  return list.filter((s) => !s.flag || flagOn(s.flag));
}
