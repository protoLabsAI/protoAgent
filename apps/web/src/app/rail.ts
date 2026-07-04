// Rail-surface helpers (ADR 0036). The rail item list is assembled from three sources — the
// user-ordered `railOrder` surfaces, plugin views not yet reconciled, and fork-contributed
// (`ext`) surfaces. The first two are mutually exclusive by construction, but `ext` surfaces are
// appended without a placement check, so a fork id that collides with a core/plugin id already in
// the list would render twice. A duplicate id is a duplicate React key AND a duplicate dnd-kit
// sortable id, which desyncs the sortable rail's index→id mapping. Dedup by id before the list
// reaches the rail. (#1755 hardening — see the issue for the separate DS-side click bug.)

export function dedupeRailById<T extends { id: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  return items.filter((it) => {
    if (seen.has(it.id)) return false;
    seen.add(it.id);
    return true;
  });
}
