// Substitute ${key} placeholders in every string of an MCP server template (args, env,
// url, headers) with the operator-supplied values. Shared utility (#2041 slice 3): the
// MCP catalog quick-add (Settings ▸ MCP) and the new-agent Configure step both fill the
// same catalog-shaped templates, so the substitution lives here rather than in either UI.
export function fillTemplate(
  template: Record<string, unknown>,
  values: Record<string, string>,
): Record<string, unknown> {
  const sub = (v: unknown): unknown => {
    if (typeof v === "string") return v.replace(/\$\{(\w+)\}/g, (_m, k: string) => values[k] ?? "");
    if (Array.isArray(v)) return v.map(sub);
    if (v && typeof v === "object") {
      return Object.fromEntries(
        Object.entries(v as Record<string, unknown>).map(([k, val]) => [k, sub(val)]),
      );
    }
    return v;
  };
  return sub(template) as Record<string, unknown>;
}
