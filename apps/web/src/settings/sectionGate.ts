// Pure half of the settings section gates. Extracted from SettingsSurface so the gating is
// unit-testable without importing the whole settings tree (secretsGate.test.ts exercises the
// flag both ways; boxSectionGate.test.ts the host axis) — the component wires `flagOn` to
// useFlagPredicate() and `onHost` to isHostConsole().
//
// Two independent axes:
//   flag    — ADR 0068 developer flags: a flag-off section is dropped.
//   hostOnly — a section whose data only means something on the host console (the Box group's
//              Overview + Telemetry read the FOCUSED agent's endpoints). Box ▸ Fleet carries
//              no `hostOnly`, so a sister agent's window keeps it — `/api/fleet` is a hub path
//              and names the same fleet from any window.
//
// Both drop a section from the nav AND from the resolvable set, so a persisted/deep-linked id
// pointing at a gated section falls back to the first visible one instead of a blank pane.
export type GatedSection = { id: string; flag?: string; hostOnly?: boolean };

export function visibleSections<T extends GatedSection>(
  list: T[],
  flagOn: (id: string) => boolean,
  onHost = true,
): T[] {
  return list.filter((s) => (!s.flag || flagOn(s.flag)) && (onHost || !s.hostOnly));
}
