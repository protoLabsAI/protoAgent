import "./settings.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { useToast } from "@protolabsai/ui/overlays";
import { Badge } from "@protolabsai/ui/primitives";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, settingsSchemaQuery } from "../lib/queries";
import type { SettingsField } from "../lib/types";
import { SettingInput } from "./SettingsCategory";
import { fieldVisible } from "./visibility";

// InlineSettings (#2000) — named settings fields rendered IN PLACE, inside the surface they
// belong to, instead of behind a chip + dialog. Same `keys` contract as <QuickSetting> and
// the same /api/settings write path; it just doesn't make the operator cross a modal to
// reach three switches.
//
// Two things it does that QuickSettingDialog didn't:
//   * honours `depends_on` via fieldVisible — a dependent gate stays hidden until its
//     parent is on, matching the canonical settings pages. (The dialog mapped over its
//     fields raw, so e.g. "Require approval per command" rendered while run_command was
//     off, presenting a switch that governed nothing.)
//   * writes on change, like every other switch on these panels — no Save button to hunt
//     for. Saves hot-rebuild the graph, so a flip applies immediately.
//
// Scope: fields are written to the layer they belong to — host-scoped fields to the host
// layer, everything else to the agent leaf — matching SettingsCategory's split rather than
// QuickSettingDialog's all-or-nothing guess.

export function InlineSettings({
  keys,
  className,
  onSaved,
}: {
  keys: string[];
  className?: string;
  /** Called after a successful write (e.g. to refetch a list the change rebuilds). */
  onSaved?: () => void;
}) {
  const queryClient = useQueryClient();
  const schema = useQuery(settingsSchemaQuery());
  const toast = useToast();
  // Values written but not yet reflected by a schema refetch. Keeps a switch tracking the
  // click through the round-trip (the save hot-rebuilds, so the refetch isn't instant).
  const [pending, setPending] = useState<Record<string, unknown>>({});

  const fields = useMemo<SettingsField[]>(() => {
    const byKey = new globalThis.Map<string, SettingsField>();
    for (const g of schema.data?.groups ?? []) for (const f of g.fields) byKey.set(f.key, f);
    return keys.map((k) => byKey.get(k)).filter((f): f is SettingsField => Boolean(f));
  }, [schema.data, keys]);

  const valueOf = (key: string) => {
    if (key in pending) return pending[key];
    for (const g of schema.data?.groups ?? []) for (const f of g.fields) if (f.key === key) return f.value;
    return undefined;
  };

  const save = useMutation({
    mutationFn: ({ field, value }: { field: SettingsField; value: unknown }) =>
      api.saveSettings({ [field.key]: value }, field.scope === "host" ? "host" : "agent"),
    onMutate: ({ field, value }) => setPending((p) => ({ ...p, [field.key]: value })),
    onSuccess: (r, { field }) => {
      if (!r.ok) {
        toast({ tone: "error", title: "Save failed", message: r.messages.join(" · ") });
        setPending((p) => { const next = { ...p }; delete next[field.key]; return next; });
        return;
      }
      const restart = r.restart_required?.length ? ` Restart required for: ${r.restart_required.join(", ")}.` : "";
      toast({ tone: "success", title: "Saved", message: `${field.label} updated.${restart}` });
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
      onSaved?.();
    },
    onError: (e, { field }) => {
      toast({ tone: "error", title: "Save failed", message: errMsg(e) });
      setPending((p) => { const next = { ...p }; delete next[field.key]; return next; });
    },
  });

  if (schema.isLoading) return <p className="muted">Loading…</p>;
  if (!fields.length) return null;

  // `depends_on` reads the LIVE value (a pending write if there is one), so turning a
  // parent off collapses its dependents in the same click.
  const visible = fields.filter((f) => fieldVisible(f, valueOf));

  return (
    <div className={`inline-settings${className ? ` ${className}` : ""}`}>
      {visible.map((field) => (
        <div className="setting-row" key={field.key} data-key={field.key}>
          <div className="setting-meta">
            <label className="setting-label" htmlFor={`set-${field.key}`}>
              {field.label}
              {field.restart ? <Badge status="warning">restart</Badge> : null}
              {field.scope === "host" ? <Badge status="info">box-shared</Badge> : null}
            </label>
            {field.description ? <p className="setting-desc">{field.description}</p> : null}
          </div>
          <div className="setting-control">
            <SettingInput
              field={field}
              value={valueOf(field.key)}
              onChange={(v) => save.mutate({ field, value: v })}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
