import "./settings.css";

import { Badge, Button } from "@protolabsai/ui/primitives";
import { Dialog } from "@protolabsai/ui/overlays";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Loader2, Save, Settings2 } from "lucide-react";
import { useMemo, useState } from "react";
import type { ReactNode } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, settingsSchemaQuery } from "../lib/queries";
import type { SettingsField } from "../lib/types";
import { SettingInput } from "./SettingsCategory";

// QuickSetting (ADR 0048) — a contextual settings shortcut: a small icon button that
// opens a dialog editing one (or a few) named fields right where they're relevant,
// instead of making the operator navigate to the central Settings home. It edits the
// SAME fields via the SAME /api/settings write path + cascade (so a host-scoped field
// saves to the host layer); the central Settings surface stays the canonical
// one-stop-shop. Drop one in anywhere:
//
//   <QuickSetting keys={["skills.scope"]} title="Skill sharing" />
//   <QuickSetting keys={["model.temperature", "model.max_tokens"]} title="Model tuning" />

export function QuickSetting({
  keys,
  title = "Quick settings",
  label,
  icon,
  deepLink,
  summaryKey,
}: {
  keys: string[];
  title?: string;
  /** Accessible label / tooltip for the trigger button (defaults to title). */
  label?: string;
  /** Trigger glyph (defaults to a gear). */
  icon?: ReactNode;
  /** Optional "Open full settings →" callback (e.g. route to the central home). */
  deepLink?: () => void;
  /** When set, the trigger renders as a CHIP showing this field's current value
   * (e.g. the model alias on the chat composer) instead of an icon-only button. */
  summaryKey?: string;
}) {
  const [open, setOpen] = useState(false);
  // Only fetch the (cached) schema when a chip summary is requested.
  const schema = useQuery({ ...settingsSchemaQuery(), enabled: Boolean(summaryKey) });
  const summary = summaryKey
    ? schema.data?.groups.flatMap((g) => g.fields).find((f) => f.key === summaryKey)?.value
    : undefined;
  const tip = label ?? title;

  return (
    <>
      {summaryKey ? (
        <Button variant="ghost" size="sm" type="button" title={tip} aria-label={tip} onClick={() => setOpen(true)}>
          {icon ?? <Settings2 size={14} />}
          <span className="quick-setting-summary">{summary != null && summary !== "" ? String(summary) : title}</span>
        </Button>
      ) : (
        <Button icon variant="ghost" type="button" title={tip} aria-label={tip} onClick={() => setOpen(true)}>
          {icon ?? <Settings2 size={15} />}
        </Button>
      )}
      {open ? (
        <QuickSettingDialog keys={keys} title={title} deepLink={deepLink} onClose={() => setOpen(false)} />
      ) : null}
    </>
  );
}

function QuickSettingDialog({
  keys,
  title,
  deepLink,
  onClose,
}: {
  keys: string[];
  title: string;
  deepLink?: () => void;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const schema = useQuery(settingsSchemaQuery());
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [status, setStatus] = useState("");

  const fields = useMemo<SettingsField[]>(() => {
    const byKey = new globalThis.Map<string, SettingsField>();
    for (const g of schema.data?.groups ?? []) for (const f of g.fields) byKey.set(f.key, f);
    return keys.map((k) => byKey.get(k)).filter((f): f is SettingsField => Boolean(f));
  }, [schema.data, keys]);

  // Save to the host layer iff every edited field is host-scoped (a mixed set is
  // unusual for a quick-set; default to the agent leaf then).
  const layer = fields.length && fields.every((f) => f.scope === "host") ? "host" : "agent";

  const save = useMutation({
    mutationFn: () => api.saveSettings(dirty, layer),
    onMutate: () => setStatus("saving…"),
    onSuccess: (r) => {
      if (!r.ok) {
        setStatus(`save failed: ${r.messages.join(" · ")}`);
        return;
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
      onClose();
    },
    onError: (e) => setStatus(`save failed: ${errMsg(e)}`),
  });

  const dirtyKeys = Object.keys(dirty);

  return (
    <Dialog
      open
      onClose={onClose}
      title={title}
      width={460}
      className="quick-setting-dialog"
      footer={
        <>
          {deepLink ? (
            <Button type="button" variant="ghost" onClick={() => { onClose(); deepLink(); }}>
              <ExternalLink size={14} /> Open full settings
            </Button>
          ) : null}
          <Button type="button" onClick={onClose} disabled={save.isPending}>Cancel</Button>
          <Button variant="primary" type="button" onClick={() => save.mutate()} disabled={save.isPending || !dirtyKeys.length}>
            {save.isPending ? <Loader2 className="spin" size={15} /> : <Save size={15} />} Save
          </Button>
        </>
      }
    >
      {schema.isLoading ? (
        <p className="muted">Loading…</p>
      ) : !fields.length ? (
        <p className="muted">Nothing to configure here.</p>
      ) : (
        <div className="quick-setting-body">
          {fields.map((field) => (
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
                  value={field.key in dirty ? dirty[field.key] : field.value}
                  onChange={(v) => setDirty((d) => ({ ...d, [field.key]: v }))}
                />
              </div>
            </div>
          ))}
        </div>
      )}
      {status ? <p className="settings-status">{status}</p> : null}
    </Dialog>
  );
}
