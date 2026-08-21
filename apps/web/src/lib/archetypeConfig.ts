import type { ArchetypePreview, BundleConfigInputType, McpCatalogEntry, McpCatalogInput } from "./types";

// The Configure step in NewAgentPanel (#2041 slice 3) and the enriched
// ArchetypePreviewDialog both read a bundle archetype's operator-facing setup off the
// preview peek: the MCP servers it will wire (each carrying its own `${input}`
// placeholders) and the standalone secrets it declares. These pure helpers turn that
// peek into (a) a flat form spec the panel renders + collects, and (b) the read-only
// summaries the preview dialog shows. No React — unit-tested directly.

// One field in the inline Configure form. `origin` records which create() channel the
// value feeds: "input" → the `inputs` map (MCP `${key}` substitution at seed time),
// "secret" → the `secrets` list (the bundle's declared secrets), "config" → the
// `config_inputs` map (values written into the config at the declared dotted keys,
// #2934). A declared secret is always masked; an MCP input is masked only when the
// catalog marks it `secret`.
export type ConfigField = {
  key: string;
  label: string;
  placeholder?: string;
  secret: boolean;
  required: boolean;
  origin: "input" | "secret" | "config";
  // The MCP server this input belongs to (context/grouping); undefined otherwise.
  server?: string;
  // config_inputs only (#2934): which widget renders the field (string/path → text,
  // delegate → dropdown of configured ACP delegates, boolean → toggle) and the
  // declared default, stringified. A blank field with a default is NOT "missing
  // required" — the backend writes the default when the operator skips it.
  kind?: BundleConfigInputType;
  defaultValue?: string;
};

// Form state is keyed by origin(+server)+key: an MCP input and a declared secret that
// happen to share a `key` don't clobber each other, and two servers that both declare the
// same input key (e.g. both need a `token`, #2128) each get their own form field. A
// declared secret has no `server`, so its id stays "secret:key".
export function fieldId(f: Pick<ConfigField, "origin" | "key" | "server">): string {
  return f.server ? `${f.origin}:${f.server}:${f.key}` : `${f.origin}:${f.key}`;
}

// Read a field's collected value. The panel keys its state by the full fieldId; a bare
// `origin:key` entry still fills a server-tagged field with no qualified entry. That
// mirrors the backend's seed-time precedence (resolve_bundle_mcp_item, #2128): a
// namespaced value wins for its server, a bare key fills any server without one.
function fieldValue(values: Record<string, string>, f: ConfigField): string {
  return (values[fieldId(f)] ?? values[`${f.origin}:${f.key}`] ?? "").trim();
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
  // Declared config_inputs (#2934) — plugin config keys the bundle asks the operator
  // for. `type` picks the widget; unknown types degrade to a plain text field so an
  // older console still collects the value.
  for (const ci of bundle.config_inputs ?? []) {
    const kind: ConfigField["kind"] =
      ci.type === "path" || ci.type === "delegate" || ci.type === "boolean" ? ci.type : "string";
    const fallback = ci.default !== undefined && ci.default !== null ? String(ci.default) : undefined;
    fields.push({
      key: ci.key,
      label: ci.label,
      placeholder: fallback,
      secret: false,
      required: Boolean(ci.required),
      origin: "config",
      kind,
      defaultValue: fallback,
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
// A config field with a declared default is never missing (the backend writes the default
// when skipped), and a boolean toggle always resolves to a value.
export function isMissingRequiredConfig(fields: ConfigField[], values: Record<string, string>): boolean {
  return fields.some(
    (f) => f.required && f.kind !== "boolean" && f.defaultValue === undefined && !fieldValue(values, f),
  );
}

// Split the collected form values back into the three create() channels. Blank values are
// dropped so the backend's env/default fallthrough (#2041/#2934) still applies to whatever
// the operator skipped — only explicitly-entered values are sent.
//
// When two servers declare the same input `key` (#2128), each colliding input goes on the
// wire as `"server:key"` — resolve_bundle_mcp_item scopes that value to its server — so
// both get their own value. A key owned by one server stays bare, keeping the wire format
// identical to today's for the common single-server bundle.
//
// A config field (#2934) goes to the `config` map keyed by its dotted config path; a
// boolean toggle's "true"/"false" form state crosses the wire as a real boolean.
export function splitConfigValues(
  fields: ConfigField[],
  values: Record<string, string>,
): {
  inputs: Record<string, string>;
  secrets: { key: string; value: string }[];
  config: Record<string, string | boolean>;
} {
  // Distinct `server` values per bare input key; ≥2 ⇒ that key collides. Purely local —
  // archetypeConfigFields already tagged every MCP input with its server.
  const serversByKey = new Map<string, Set<string>>();
  for (const f of fields) {
    if (f.origin !== "input") continue;
    const set = serversByKey.get(f.key) ?? new Set<string>();
    set.add(f.server ?? "");
    serversByKey.set(f.key, set);
  }
  const inputs: Record<string, string> = {};
  const secrets: { key: string; value: string }[] = [];
  const config: Record<string, string | boolean> = {};
  for (const f of fields) {
    const v = fieldValue(values, f);
    if (!v) continue;
    if (f.origin === "secret") {
      secrets.push({ key: f.key, value: v });
    } else if (f.origin === "config") {
      config[f.key] = f.kind === "boolean" ? v === "true" : v;
    } else {
      const collides = (serversByKey.get(f.key)?.size ?? 0) > 1;
      inputs[collides && f.server ? `${f.server}:${f.key}` : f.key] = v;
    }
  }
  return { inputs, secrets, config };
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
