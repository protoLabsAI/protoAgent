import { QueryErrorResetBoundary, useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { AlertTriangle, RotateCcw, Save } from "lucide-react";
import { Suspense, useMemo, useState } from "react";

import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { api } from "../lib/api";
import { queryKeys, settingsSchemaQuery } from "../lib/queries";
import type { SettingsField } from "../lib/types";

// Generic settings surface — renders whatever GET /api/settings/schema returns,
// so it stays in sync as config grows. Saving POSTs the changed fields and the
// server hot-reloads the agent; fields flagged `restart` get a badge + banner.
// On the TanStack Query data layer (ADR 0013): the schema is a useSuspenseQuery,
// save is a useMutation that invalidates it; loading/errors via Suspense +
// ErrorBoundary.

export function SettingsSurface() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="settings" />}>
            <Suspense fallback={<PanelSkeleton label="Loading settings…" />}>
              <SettingsBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}

function SettingsBody() {
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery(settingsSchemaQuery());
  const groups = data.groups;
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [status, setStatus] = useState("");

  const dirtyKeys = Object.keys(dirty);

  // Pending changes that won't take effect until a process restart.
  const pendingRestart = useMemo(() => {
    const labels: string[] = [];
    for (const g of groups) {
      for (const f of g.fields) {
        if (f.restart && f.key in dirty) labels.push(f.label);
      }
    }
    return labels;
  }, [groups, dirty]);

  const save = useMutation({
    mutationFn: () => api.saveSettings(dirty),
    onMutate: () => setStatus("saving…"),
    onSuccess: (r) => {
      if (!r.ok) {
        setStatus(`save failed: ${r.messages.join(" · ")}`);
        return;
      }
      const restartNote = r.restart_required.length
        ? ` — restart required for: ${r.restart_required.join(", ")}`
        : "";
      setStatus(`${r.messages.join(" · ")}${restartNote}`);
      setDirty({});
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
    onError: (e) => setStatus(`save failed: ${e instanceof Error ? e.message : String(e)}`),
  });

  function discard() {
    setDirty({});
    setStatus("");
  }

  return (
    <>
      <div className="panel-header">
        <div>
          <h1>Settings</h1>
          <p className="panel-kicker">
            {dirtyKeys.length ? `${dirtyKeys.length} unsaved change${dirtyKeys.length === 1 ? "" : "s"}` : "applies on save"}
          </p>
        </div>
        <div className="settings-actions">
          <button className="secondary-button" type="button" onClick={discard} disabled={save.isPending || !dirtyKeys.length}>
            <RotateCcw size={15} />
            Discard
          </button>
          <button className="primary-button" type="button" onClick={() => save.mutate()} disabled={save.isPending || !dirtyKeys.length}>
            <Save size={16} />
            Save &amp; apply
          </button>
        </div>
      </div>
      <div className="stage-body">
        {pendingRestart.length ? (
          <div className="settings-banner" role="alert">
            <AlertTriangle size={14} />
            <span>Needs a restart to take effect: {pendingRestart.join(", ")}</span>
          </div>
        ) : null}
        {status ? <p className="settings-status">{status}</p> : null}

        {groups.map((group) => (
          <section className="settings-group" key={group.section}>
            <p className="settings-group-title">{group.section}</p>
            {group.fields.map((field) => (
              <SettingRow
                key={field.key}
                field={field}
                dirty={field.key in dirty}
                value={field.key in dirty ? dirty[field.key] : field.value}
                onChange={(v) => setDirty((d) => ({ ...d, [field.key]: v }))}
              />
            ))}
          </section>
        ))}
      </div>
    </>
  );
}

function SettingRow({
  field,
  value,
  dirty,
  onChange,
}: {
  field: SettingsField;
  value: unknown;
  dirty: boolean;
  onChange: (value: unknown) => void;
}) {
  return (
    <div className={`setting-row${dirty ? " dirty" : ""}`} data-key={field.key}>
      <div className="setting-meta">
        <label className="setting-label" htmlFor={`set-${field.key}`}>
          {field.label}
          {field.restart ? <span className="setting-restart">restart</span> : null}
        </label>
        {field.description ? <p className="setting-desc">{field.description}</p> : null}
      </div>
      <div className="setting-control">
        <SettingInput field={field} value={value} onChange={onChange} />
      </div>
    </div>
  );
}

function SettingInput({
  field,
  value,
  onChange,
}: {
  field: SettingsField;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const id = `set-${field.key}`;

  if (field.type === "bool") {
    return (
      <label className="setting-toggle">
        <input
          id={id}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>{value ? "on" : "off"}</span>
      </label>
    );
  }

  if (field.type === "number") {
    return (
      <input
        id={id}
        className="setting-input"
        type="number"
        value={value === undefined || value === null ? "" : String(value)}
        min={field.minimum}
        max={field.maximum}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    );
  }

  if (field.type === "select" && field.options.length) {
    return (
      <select id={id} className="setting-input" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
        {field.options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    );
  }

  if (field.type === "string_list") {
    const text = Array.isArray(value) ? value.join("\n") : "";
    return (
      <textarea
        id={id}
        className="setting-input setting-textarea"
        rows={3}
        value={text}
        placeholder="one per line"
        onChange={(e) =>
          onChange(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))
        }
      />
    );
  }

  if (field.type === "secret") {
    return (
      <input
        id={id}
        className="setting-input"
        type="password"
        autoComplete="new-password"
        value={typeof value === "string" ? value : ""}
        placeholder={field.is_set ? "•••••••• (set — leave blank to keep)" : "unset"}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  // string + select-without-options fallback
  return (
    <input
      id={id}
      className="setting-input"
      type="text"
      value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
