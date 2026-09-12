// The wizard's post-install dependency report (#3450).
//
// It used to read `/api/plugins/installed` alone, which enumerates the live plugins dir
// plus plugins.lock — so a BUNDLED plugin has no row there at all, and a bundled plugin
// that declares pip deps was invisible to the report. That is not a corner case: the
// Cowork archetype enables the bundled `cowork` pack, whose document skills import
// python-docx / openpyxl / python-pptx / reportlab / pypdf inside `execute_code`. On a
// fresh server a first run finished "ready" with four of the five absent, and the first
// symptom was an ImportError in the middle of the operator's first real task.
//
// The runtime status carries the loader's own per-plugin meta — every plugin it saw,
// bundled included, with the same `deps_missing` the route computes — so the two
// together cover both kinds. Pure and separate so the merge is unit-tested rather than
// asserted through the wizard's whole finish path.

/** One row of the report: a just-enabled plugin whose declared deps aren't installed. */
export type DepsReportRow = { id: string; name: string; deps: string[] };

type InstalledRow = { id: string; deps_missing?: string[] | null; manifest?: { name?: string } | null };
type RuntimePluginRow = { id: string; name?: string; deps_missing?: string[] | null };

/**
 * The just-enabled plugins that are missing declared pip deps, in `enabledIds` order.
 *
 * `installed` wins where a plugin appears in both (it is computed per request and
 * carries the manifest name); `runtime` is the only source for a bundled plugin. A
 * plugin the wizard did not just enable is never reported — the report exists to explain
 * what this setup run left half-provisioned, not to audit the instance.
 */
export function needyPlugins(
  enabledIds: readonly string[],
  installed: readonly InstalledRow[],
  runtime: readonly RuntimePluginRow[],
): DepsReportRow[] {
  const installedById = new Map(installed.map((p) => [p.id, p]));
  const runtimeById = new Map(runtime.map((p) => [p.id, p]));
  const rows: DepsReportRow[] = [];
  const seen = new Set<string>();
  for (const id of enabledIds) {
    if (seen.has(id)) continue;
    seen.add(id);
    const inst = installedById.get(id);
    const meta = runtimeById.get(id);
    const deps = (inst?.deps_missing?.length ? inst.deps_missing : meta?.deps_missing) ?? [];
    if (!deps.length) continue;
    rows.push({ id, name: inst?.manifest?.name || meta?.name || id, deps: [...deps] });
  }
  return rows;
}
