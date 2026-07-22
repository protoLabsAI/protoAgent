import type { ArchetypePreview, McpCatalogEntry, McpCatalogInput } from "./types";

// The Configure step in NewAgentPanel (#2041 slice 3) and the enriched
// ArchetypePreviewDialog both read a bundle archetype's operator-facing setup off the
// preview peek: the MCP servers it will wire (each carrying its own `${input}`
// placeholders) and the standalone secrets it declares. These pure helpers turn that
// peek into (a) a flat form spec the panel renders + collects, and (b) the read-only
// summaries the preview dialog shows. No React — unit-tested directly.

// One field in the inline Configure form. `origin` records which create() channel the
// value feeds: "input" → the `inputs` map (MCP `${key}` substitution at seed time),
// "secret" → the `secrets` list (the bundle's declared secrets). A declared secret is
// always masked; an MCP input is masked only when the catalog marks it `secret`.
export type ConfigField = {
  key: string;
  label: string;
  placeholder?: string;
  secret: boolean;
  required: boolean;
  origin: "input" | "secret";
  // The MCP server this input belongs to (context/grouping); undefined for a declared secret.
  server?: string;
};

// Form state is keyed by origin+key so an MCP input and a declared secret that happen to
// share a `key` don't clobber each other in the value map.
export function fieldId(f: Pick<ConfigField, "origin" | "key">): string {
  return `${f.origin}:${f.key}`;
}

// Flatten a bundle preview into the Configure form's fields: each MCP server's inputs
// first, then the bundle's declared secrets. Empty when the archetype has neither (no
// bundle, or a bundle with no inputs/secrets) → the panel shows no form (backward compat
// with input-free archetypes).
export function archetypeConfigFields(preview: ArchetypePreview | undefined): ConfigField[] {
  const bundle = preview?.bundle;
  if (!bundle) return [];
  const fields: ConfigField[] = [];
  for (const item of bundle.mcp ?? []) {
    for (const inp of item.inputs ?? []) {
      fields.push({
        key: inp.key,
        label: inp.label,
        placeholder: inp.placeholder,
        secret: Boolean(inp.secret),
        required: Boolean(inp.required),
        origin: "input",
        server: item.name,
      });
    }
  }
  for (const sec of bundle.secrets ?? []) {
    fields.push({
      key: sec.key,
      label: sec.label,
      placeholder: sec.placeholder,
      secret: true, // a declared secret is always masked
      required: Boolean(sec.required),
      origin: "secret",
    });
  }
  return fields;
}

// Anything to configure? Drives whether the inline form is offered at all.
export function hasConfigFields(preview: ArchetypePreview | undefined): boolean {
  return archetypeConfigFields(preview).length > 0;
}

// A required field left blank blocks create while the form is OPEN — the operator either
// fills it or collapses the form to skip (→ env-only). Trims so whitespace isn't "filled".
export function configMissingRequired(fields: ConfigField[], values: Record<string, string>): boolean {
  return fields.some((f) => f.required && !(values[fieldId(f)] ?? "").trim());
}

// Split the collected form values back into the two create() channels. Blank values are
// dropped so the backend's env/default fallthrough (#2041) still applies to whatever the
// operator skipped — only explicitly-entered values are sent.
export function splitConfigValues(
  fields: ConfigField[],
  values: Record<string, string>,
): { inputs: Record<string, string>; secrets: { key: string; value: string }[] } {
  const inputs: Record<string, string> = {};
  const secrets: { key: string; value: string }[] = [];
  for (const f of fields) {
    const v = (values[fieldId(f)] ?? "").trim();
    if (!v) continue;
    if (f.origin === "secret") secrets.push({ key: f.key, value: v });
    else inputs[f.key] = v;
  }
  return { inputs, secrets };
}

// ── Read-only preview summaries (ArchetypePreviewDialog) ──────────────────────────────
// "GitHub (needs token)" — the server name, annotated with what it needs when the
// catalog entry declares a `requires`.
export function mcpItemLabel(item: McpCatalogEntry): string {
  return item.requires ? `${item.name} (needs ${item.requires})` : item.name;
}

// "GitHub (needs token), Brave Search (needs API key)"
export function previewMcpSummary(mcp: McpCatalogEntry[] | undefined): string {
  return (mcp ?? []).map(mcpItemLabel).join(", ");
}

// "GitHub token, Brave API key"
export function previewSecretsSummary(secrets: McpCatalogInput[] | undefined): string {
  return (secrets ?? []).map((s) => s.label).join(", ");
}
