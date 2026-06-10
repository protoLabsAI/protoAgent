import "./delegates.css";

import { Input, Select, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil, Plug, Plus, ShieldCheck, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";

import { api } from "../lib/api";
import { delegatesQuery, delegateTypesQuery, queryKeys } from "../lib/queries";
import type { DelegateFieldSpec, DelegateProbe, DelegateTypeSpec, DelegateView } from "../lib/types";

// Delegates panel (ADR 0025, PR3) — manage the agents & endpoints the agent can
// talk to via delegate_to, under Settings → Integrations. Hot-swappable: create/
// edit/delete write config + secrets and the server reloads, so changes take
// effect on the next turn. Read non-suspense so a 404 (plugin disabled) shows a
// hint rather than blanking Settings.

const DELEGATES_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/delegates";

// ── dotted-key helpers (delegate fields use keys like "auth.token") ───────────
function setDotted(obj: Record<string, unknown>, key: string, val: unknown): void {
  const parts = key.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const k = parts[i];
    if (typeof cur[k] !== "object" || cur[k] === null) cur[k] = {};
    cur = cur[k] as Record<string, unknown>;
  }
  cur[parts[parts.length - 1]] = val;
}
function getDotted(obj: unknown, key: string): unknown {
  return key.split(".").reduce<unknown>((cur, k) => (cur == null ? undefined : (cur as Record<string, unknown>)[k]), obj);
}

function coerce(field: DelegateFieldSpec, raw: unknown): unknown {
  if (field.kind === "args") {
    return String(raw ?? "").split(/\s+/).filter(Boolean);
  }
  if (field.kind === "number") {
    return raw === "" || raw == null ? undefined : Number(raw);
  }
  return typeof raw === "string" ? raw : raw == null ? "" : String(raw);
}

function probeLine(p: DelegateProbe): string {
  if (p.ok) {
    const lat = p.latency_ms != null ? ` (${p.latency_ms} ms)` : "";
    return `✓ ${p.detail || "reachable"}${lat}`;
  }
  return `✗ ${p.error || "unreachable"}`;
}

export function DelegatesSection() {
  const qc = useQueryClient();
  const list = useQuery(delegatesQuery());
  const types = useQuery(delegateTypesQuery());
  const [editing, setEditing] = useState<DelegateView | null>(null);
  const [adding, setAdding] = useState(false);
  const [status, setStatus] = useState("");
  const [probes, setProbes] = useState<Record<string, DelegateProbe>>({});

  const invalidate = () => qc.invalidateQueries({ queryKey: queryKeys.delegates });

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteDelegate(name),
    onSuccess: (r) => {
      setStatus(r.message || "deleted");
      void invalidate();
    },
    onError: (e) => setStatus(`delete failed: ${e instanceof Error ? e.message : String(e)}`),
  });

  const testRow = useMutation({
    mutationFn: (d: DelegateView) => api.testDelegate({ name: d.name, type: d.type }),
    onSuccess: (p, d) => setProbes((m) => ({ ...m, [d.name]: p })),
    onError: (e, d) => setProbes((m) => ({ ...m, [d.name]: { ok: false, error: e instanceof Error ? e.message : String(e) } })),
  });

  // The delegates plugin isn't enabled (routes 404) — show a hint, not an error.
  if (list.isError) {
    return (
      <section className="settings-group">
        <p className="settings-group-title">Delegates</p>
        <p className="setting-desc">
          Enable the <code>delegates</code> plugin (<code>plugins: {"{ enabled: [delegates] }"}</code>) to
          manage the agents and endpoints this agent can talk to.{" "}
          <a className="settings-help-link" href={DELEGATES_GUIDE_URL} target="_blank" rel="noreferrer">
            Guide
          </a>
        </p>
      </section>
    );
  }

  const delegates = list.data?.delegates ?? [];
  const typeSpecs = types.data?.types ?? [];

  return (
    <section className="settings-group delegates-section">
      <p className="settings-group-title">Delegates</p>
      <p className="setting-desc">
        Agents &amp; endpoints this agent can reach via <code>delegate_to</code> — changes apply on the next turn.
      </p>

      <div className="subagent-list">
        {delegates.map((d) => {
          const p = probes[d.name];
          return (
            <div className="subagent-row" key={d.name}>
              <div>
                <strong>
                  {d.health ? (
                    <span
                      className={`delegate-health ${d.health.ok ? "ok" : d.health.ok === false ? "down" : "unknown"}`}
                      title={d.health.ok
                        ? `${d.health.detail || "reachable"}${d.health.latency_ms != null ? ` (${d.health.latency_ms} ms)` : ""}`
                        : d.health.error || "unreachable"}
                    >
                      ●
                    </span>
                  ) : null}
                  {d.name} <span className="delegate-type-badge">{d.type}</span>
                  {!d.configured ? <span className="delegate-badge-warn">⚠ unconfigured</span> : null}
                  {d.has_secret ? <span className="delegate-badge-ok">secret set</span> : null}
                </strong>
                <span>{p ? probeLine(p) : d.description || d.error || ""}</span>
              </div>
              <div className="issue-actions">
                <Button icon variant="ghost" title="Test" onClick={() => testRow.mutate(d)} disabled={testRow.isPending}>
                  {testRow.isPending && testRow.variables?.name === d.name ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                </Button>
                <Button icon variant="ghost" title="Edit" onClick={() => { setEditing(d); setAdding(false); }}>
                  <Pencil size={15} />
                </Button>
                <Button icon variant="ghost" title="Delete" onClick={() => remove.mutate(d.name)} disabled={remove.isPending}>
                  <Trash2 size={15} />
                </Button>
              </div>
            </div>
          );
        })}
        {!delegates.length ? <p className="setting-desc">No delegates yet — add one below.</p> : null}
      </div>

      {status ? <p className="settings-inline-status">{status}</p> : null}

      {editing ? (
        <DelegateForm
          key={editing.name}
          spec={typeSpecs}
          initial={editing}
          onClose={() => setEditing(null)}
          onSaved={(msg) => { setEditing(null); setStatus(msg); void invalidate(); }}
        />
      ) : adding ? (
        <DelegateForm
          spec={typeSpecs}
          initial={null}
          onClose={() => setAdding(false)}
          onSaved={(msg) => { setAdding(false); setStatus(msg); void invalidate(); }}
        />
      ) : (
        <div className="settings-group-actions">
          <Button type="button" onClick={() => setAdding(true)} disabled={!typeSpecs.length}>
            <Plus size={15} /> Add delegate
          </Button>
        </div>
      )}
    </section>
  );
}

function DelegateForm({
  spec,
  initial,
  onClose,
  onSaved,
}: {
  spec: DelegateTypeSpec[];
  initial: DelegateView | null;
  onClose: () => void;
  onSaved: (msg: string) => void;
}) {
  const editing = Boolean(initial);
  const [type, setType] = useState(initial?.type || spec[0]?.type || "a2a");
  const [name, setName] = useState(initial?.name || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [vals, setVals] = useState<Record<string, string>>(() => seed(initial, spec));
  const [probe, setProbe] = useState<DelegateProbe | null>(null);
  const [err, setErr] = useState("");

  const current = useMemo(() => spec.find((s) => s.type === type), [spec, type]);

  function buildEntry(): Record<string, unknown> {
    const entry: Record<string, unknown> = { name, type, description };
    for (const f of current?.fields ?? []) {
      const v = coerce(f, vals[f.key]);
      // skip blank secrets on edit so we don't overwrite a stored one with ""
      if (f.kind === "secret" && (v === "" || v == null)) continue;
      if (v === "" || v == null) continue;
      setDotted(entry, f.key, v);
    }
    return entry;
  }

  const test = useMutation({
    mutationFn: () => api.testDelegate(buildEntry()),
    onSuccess: (p) => { setProbe(p); setErr(""); },
    onError: (e) => setErr(e instanceof Error ? e.message : String(e)),
  });

  const save = useMutation({
    mutationFn: () => (editing ? api.updateDelegate(name, buildEntry()) : api.createDelegate(buildEntry())),
    onSuccess: (r) => onSaved(r.message || (editing ? "updated" : "created")),
    onError: (e) => setErr(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="delegate-form">
      <div className="delegate-form-head">
        <strong>{editing ? `Edit ${initial?.name}` : "New delegate"}</strong>
        <Button icon variant="ghost" title="Cancel" onClick={onClose}><X size={15} /></Button>
      </div>

      {!editing ? (
        <div className="delegate-type-picker">
          {spec.map((s) => (
            <button
              key={s.type}
              type="button"
              className={`delegate-type-tile${type === s.type ? " active" : ""}`}
              onClick={() => { setType(s.type); setProbe(null); }}
            >
              <span className="delegate-type-tile-label">{s.label}</span>
              <span className="delegate-type-tile-blurb">{s.blurb}</span>
            </button>
          ))}
        </div>
      ) : null}

      <label className="field">
        <span>Name</span>
        <Input value={name} disabled={editing} onChange={(e) => setName(e.target.value)} placeholder="e.g. opus" />
      </label>
      <label className="field">
        <span>Description</span>
        <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What it's for (the model reads this to pick it)." />
      </label>

      {(current?.fields ?? []).map((f) => (
        <DelegateField
          key={f.key}
          field={f}
          value={vals[f.key] ?? ""}
          hasStoredSecret={editing && f.kind === "secret" && Boolean(initial?.has_secret)}
          onChange={(v) => setVals((m) => ({ ...m, [f.key]: v }))}
        />
      ))}

      {probe ? <p className="settings-inline-status">{probeLine(probe)}</p> : null}
      {err ? <p className="settings-status">{err}</p> : null}

      <div className="settings-group-actions">
        <Button type="button" onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? <Loader2 className="spin" size={15} /> : <Plug size={15} />} Test
        </Button>
        <Button type="button" onClick={onClose}>Cancel</Button>
        <Button variant="primary" type="button" onClick={() => save.mutate()} disabled={save.isPending || !name.trim()}>
          {save.isPending ? <Loader2 className="spin" size={15} /> : null} Save
        </Button>
      </div>
    </div>
  );
}

function DelegateField({
  field,
  value,
  hasStoredSecret,
  onChange,
}: {
  field: DelegateFieldSpec;
  value: string;
  hasStoredSecret: boolean;
  onChange: (v: string) => void;
}) {
  const common = { id: `del-${field.key}`, value, onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => onChange(e.target.value) };
  let control: React.ReactNode;
  if (field.kind === "select" && field.options.length) {
    control = (
      <Select {...common}>
        {field.options.map((o) => <option key={o} value={o}>{o || "(none)"}</option>)}
      </Select>
    );
  } else if (field.kind === "textarea") {
    control = <Textarea rows={3} placeholder={field.placeholder} {...common} />;
  } else if (field.kind === "secret") {
    control = (
      <Input
        type="password"
        autoComplete="new-password"
        placeholder={hasStoredSecret ? "•••••••• (set — leave blank to keep)" : field.placeholder || "unset"}
        {...common}
      />
    );
  } else if (field.kind === "number") {
    control = <Input type="number" placeholder={field.placeholder} {...common} />;
  } else {
    control = <Input type="text" placeholder={field.placeholder} {...common} />;
  }
  return (
    <label className="field">
      <span>{field.label}{field.required ? " *" : ""}</span>
      {control}
      {field.help ? <small className="delegate-field-help">{field.help}</small> : null}
    </label>
  );
}

function seed(initial: DelegateView | null, spec: DelegateTypeSpec[]): Record<string, string> {
  const out: Record<string, string> = {};
  if (!initial) return out;
  const t = spec.find((s) => s.type === initial.type);
  for (const f of t?.fields ?? []) {
    const v = getDotted(initial, f.key);
    if (f.kind === "args" && Array.isArray(v)) out[f.key] = v.join(" ");
    else if (f.kind === "secret") out[f.key] = ""; // redacted; blank = keep stored
    else if (v != null) out[f.key] = String(v);
  }
  return out;
}
