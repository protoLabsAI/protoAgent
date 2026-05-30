import { AlertTriangle, RefreshCw, RotateCcw, Save } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import type { SettingsField, SettingsGroup } from "../lib/types";

// Generic settings surface — renders whatever GET /api/settings/schema returns,
// so it stays in sync as config grows. Saving POSTs the changed fields and the
// server hot-reloads the agent; fields flagged `restart` get a badge + banner.

export function SettingsSurface({ onError }: { onError: (message: string) => void }) {
  const [groups, setGroups] = useState<SettingsGroup[] | null>(null);
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");

  async function load() {
    try {
      const r = await api.settingsSchema();
      setGroups(r.groups);
      setDirty({});
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  }
  useEffect(() => {
    void load();
  }, []);

  const dirtyKeys = Object.keys(dirty);

  // Pending changes that won't take effect until a process restart.
  const pendingRestart = useMemo(() => {
    if (!groups) return [];
    const labels: string[] = [];
    for (const g of groups) {
      for (const f of g.fields) {
        if (f.restart && f.key in dirty) labels.push(f.label);
      }
    }
    return labels;
  }, [groups, dirty]);

  async function save() {
    if (!dirtyKeys.length) return;
    setSaving(true);
    setStatus("saving…");
    onError("");
    try {
      const r = await api.saveSettings(dirty);
      if (!r.ok) {
        onError(r.messages.join(" · "));
        setStatus("save failed");
        return;
      }
      const restartNote = r.restart_required.length
        ? ` — restart required for: ${r.restart_required.join(", ")}`
        : "";
      setStatus(`${r.messages.join(" · ")}${restartNote}`);
      await load();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
      setStatus("save failed");
    } finally {
      setSaving(false);
    }
  }

  if (!groups) {
    return (
      <section className="panel stage-panel">
        <div className="panel-header">
          <h1>Settings</h1>
        </div>
        <div className="stage-body">
          <div className="empty-state">
            <RefreshCw size={16} className="spin" />
            <span>Loading settings…</span>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="panel stage-panel">
      <div className="panel-header">
        <div>
          <h1>Settings</h1>
          <p className="panel-kicker">
            {dirtyKeys.length ? `${dirtyKeys.length} unsaved change${dirtyKeys.length === 1 ? "" : "s"}` : "applies on save"}
          </p>
        </div>
        <div className="settings-actions">
          <button className="secondary-button" type="button" onClick={() => void load()} disabled={saving || !dirtyKeys.length}>
            <RotateCcw size={15} />
            Discard
          </button>
          <button className="primary-button" type="button" onClick={() => void save()} disabled={saving || !dirtyKeys.length}>
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
    </section>
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
