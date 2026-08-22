import { useQuery } from "@tanstack/react-query";

import { DropdownSelect, Input, SecretInput, Switch } from "@protolabsai/ui/forms";

import { fieldId, type ConfigField } from "../lib/archetypeConfig";
import { delegatesQuery } from "../lib/queries";

// The input widget for one Configure-step field — shared by SetupWizard and
// NewAgentPanel so a bundle's MCP inputs, declared secrets, and config_inputs
// (#2934) render identically in both create flows. The widget follows the field:
// a masked input for secrets, a dropdown of configured ACP delegates for
// `type: delegate`, a toggle for `type: boolean`, and a plain text input for
// string/path (path validation is OS-dependent, so no picker). Form state stays
// the caller's string map keyed by fieldId — a toggle stores "true"/"false" and
// splitConfigValues turns it back into a real boolean on the wire.
export function ArchetypeConfigField({
  field,
  value,
  onChange,
}: {
  field: ConfigField;
  value: string;
  onChange: (value: string) => void;
}) {
  // Configured delegates for the `type: delegate` dropdown — fetched only when such a
  // field is actually on screen; react-query dedupes the shared key across fields.
  const delegates = useQuery({ ...delegatesQuery(), enabled: field.kind === "delegate" });

  if (field.kind === "boolean") {
    // Untouched toggle shows the declared default; flipping it records an explicit answer.
    const checked = value ? value === "true" : field.defaultValue === "true";
    return (
      <Switch
        id={fieldId(field)}
        checked={checked}
        onCheckedChange={(v: boolean) => onChange(v ? "true" : "false")}
        aria-label={field.label}
      />
    );
  }
  if (field.kind === "delegate") {
    // Only CODING (acp) delegates can take a build — an a2a peer or an openai endpoint in
    // this list would be picked, written as the coder, and fail at first dispatch.
    const names = (delegates.data?.delegates ?? []).filter((d) => d.type === "acp").map((d) => d.name);
    return (
      <DropdownSelect
        id={fieldId(field)}
        value={value}
        onValueChange={onChange}
        options={[
          { value: "", label: names.length ? "Pick a coding delegate…" : "No coding (acp) delegates configured" },
          ...names.map((n) => ({ value: n, label: n })),
        ]}
      />
    );
  }
  if (field.secret) {
    return (
      <SecretInput
        placeholder={field.placeholder}
        value={value}
        aria-label={field.label}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <Input
      type="text"
      placeholder={field.placeholder}
      value={value}
      aria-label={field.label}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
