import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../lib/api";
import { queryKeys, settingsSchemaQuery } from "../lib/queries";

// The composer's inline model picker — rendered in the DS PromptInput `actions` slot
// (the design-system composer pattern), replacing the separate QuickSetting model chip.
// Reads the `model.name` field straight from the settings schema (its `options` are the
// available models, `value` the active one, `scope` the cascade layer) and saves on change
// via the same /api/settings write path the central Settings home uses. Temperature /
// max-tokens live in full Settings now — the composer just picks the model.
export function ComposerModelSelect() {
  const qc = useQueryClient();
  const schema = useQuery(settingsSchemaQuery());
  const field = schema.data?.groups.flatMap((g) => g.fields).find((f) => f.key === "model.name");

  const save = useMutation({
    mutationFn: (v: string) =>
      api.saveSettings({ "model.name": v }, field?.scope === "host" ? "host" : "agent"),
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.settings }),
  });

  if (!field) return null;
  const value = String(field.value ?? "");
  const options = field.options?.length ? field.options : value ? [value] : [];

  return (
    <select
      aria-label="Model"
      title="Model"
      className="composer-model-select"
      value={value}
      onChange={(e) => save.mutate(e.target.value)}
      disabled={save.isPending}
    >
      {options.map((m) => (
        <option key={m} value={m}>
          {m}
        </option>
      ))}
    </select>
  );
}
