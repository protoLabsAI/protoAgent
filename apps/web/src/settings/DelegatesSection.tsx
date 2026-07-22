import "./settings.css";
import "./delegates.css";

import { DropdownSelect, Input, RadioCard, RadioCardGroup, SecretInput, Textarea } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { Dialog, useToast } from "@protolabsai/ui/overlays";

import { StatusDot } from "@protolabsai/ui/data";

import { StatusPill } from "../app/StatusPill";
import { HelpLink } from "../app/ui-kit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Lock, Pencil, Plug, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { acpAgentsQuery, delegatesQuery, delegateTypesQuery, queryKeys } from "../lib/queries";
import type { DelegateFieldSpec, DelegateProbe, DelegateTypeSpec, DelegateView } from "../lib/types";
import { SettingsSubPanel } from "./SettingsSubPanel";

// Delegates panel (ADR 0025, PR3) — manage the agents & endpoints the agent can
// talk to via delegate_to, under Settings → Capabilities. Hot-swappable: create/
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

// ── per-delegate env editor (#2114) ──────────────────────────────────────────
// One env var as the form state models it. A `secret` row routes its value to
// secrets.yaml on save; on edit it seeds masked (value blank) — blank = keep stored.
type EnvRow = { key: string; value: string; secret: boolean };

// Seed the env rows from a DelegateView. A masked value ("***") marks a stored
// secret — seed the row secret + blank so we don't echo the mask back on save.
function seedEnvRows(initial: DelegateView | null): EnvRow[] {
  const env = initial?.env;
  if (!env || typeof env !== "object" || Array.isArray(env)) return [];
  return Object.entries(env as Record<string, unknown>).map(([key, v]) => {
    const masked = v === "***";
    return { key, value: masked ? "" : String(v ?? ""), secret: masked };
  });
}

// env_remove seeds as a comma-separated string for the list editor.
function seedEnvRemove(initial: DelegateView | null): string {
  const er = initial?.env_remove;
  return Array.isArray(er) ? er.map((x) => String(x)).join(", ") : "";
}

function probeLine(p: DelegateProbe): string {
  if (p.ok) {
    const lat = p.latency_ms != null ? ` (${p.latency_ms} ms)` : "";
    return `${p.detail || "reachable"}${lat}`;
  }
  return `${p.error || "unreachable"}`;
}

export function DelegatesSection() {
  const qc = useQueryClient();
  const list = useQuery(delegatesQuery());
  const types = useQuery(delegateTypesQuery());
  const [editing, setEditing] = useState<DelegateView | null>(null);
  const [adding, setAdding] = useState(false);
  // Per-row probe chips (test results) stay inline; transient add/remove/save feedback toasts.
  const [probes, setProbes] = useState<Record<string, DelegateProbe>>({});
  const toast = useToast();

  const invalidate = () => qc.invalidateQueries({ queryKey: queryKeys.delegates });
  const closeForm = () => { setAdding(false); setEditing(null); };

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteDelegate(name),
    onSuccess: (r) => {
      toast({ tone: "success", title: "Delegate removed", message: r.message || "Removed." });
      void invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "Remove failed", message: errMsg(e) }),
  });

  const testRow = useMutation({
    mutationFn: (d: DelegateView) => api.testDelegate({ name: d.name, type: d.type }),
    onSuccess: (p, d) => setProbes((m) => ({ ...m, [d.name]: p })),
    onError: (e, d) => setProbes((m) => ({ ...m, [d.name]: { ok: false, error: errMsg(e) } })),
  });

  // The delegate registry is built-in, so this normally resolves. A 404 here means the
  // registry route is unreachable (e.g. an older remote fleet agent) — show a hint, not
  // an error.
  if (list.isError) {
    return (
      <SettingsSubPanel label="delegates" title="Delegates">
        <p className="setting-desc">
          Couldn't reach the delegate registry for this agent — manage the agents and
          endpoints it can talk to once it's available.{" "}
          <HelpLink href={DELEGATES_GUIDE_URL}>Guide</HelpLink>
        </p>
      </SettingsSubPanel>
    );
  }

  const delegates = list.data?.delegates ?? [];
  const typeSpecs = types.data?.types ?? [];

  return (
    <SettingsSubPanel label="delegates" title="Delegates">
      <div className="delegates-section">
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
                        title={d.health.ok
                          ? `${d.health.detail || "reachable"}${d.health.latency_ms != null ? ` (${d.health.latency_ms} ms)` : ""}`
                          : d.health.error || "unreachable"}
                      >
                        <StatusDot status={d.health.ok ? "success" : d.health.ok === false ? "error" : "neutral"} />
                      </span>
                    ) : null}
                    {d.name} <Badge status="neutral">{d.type}</Badge>
                    {!d.configured ? <StatusPill label="unconfigured" tone="warning" /> : null}
                    {d.has_secret ? <StatusPill label="secret set" tone="muted" /> : null}
                  </strong>
                  <span>{p ? probeLine(p) : d.description || d.error || ""}</span>
                </div>
                <div className="issue-actions">
                  <Button icon variant="ghost" title="Test" onClick={() => testRow.mutate(d)} loading={testRow.isPending && testRow.variables?.name === d.name} disabled={testRow.isPending}>
                    <ShieldCheck size={15} />
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

        <div className="settings-group-actions">
          <Button type="button" onClick={() => { setEditing(null); setAdding(true); }} disabled={!typeSpecs.length}>
            <Plus size={15} /> Add delegate
          </Button>
        </div>

        {/* Add / edit happen in a dialog (the form used to render inline and push the
            panel down). The DS Dialog supplies the header + close, so DelegateForm
            carries only the fields + actions. */}
        <Dialog
          open={adding || editing != null}
          onClose={closeForm}
          title={editing ? `Edit ${editing.name}` : "Add a delegate"}
          width="min(560px, 94vw)"
          className="delegate-dialog"
        >
          <DelegateForm
            key={editing?.name ?? "_new"}
            spec={typeSpecs}
            initial={editing}
            onClose={closeForm}
            onSaved={(msg) => { closeForm(); toast({ tone: "success", title: "Delegate saved", message: msg }); void invalidate(); }}
          />
        </Dialog>
      </div>
    </SettingsSubPanel>
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
  // The env editor (#2114) is form-level state (not in `vals`): the `envmap` field
  // drives key/value/secret rows plus the env_remove list.
  const [envRows, setEnvRows] = useState<EnvRow[]>(() => seedEnvRows(initial));
  const [envRemove, setEnvRemove] = useState(() => seedEnvRemove(initial));
  const [preset, setPreset] = useState(""); // ACP coding-agent preset (fills command/args)
  const [probe, setProbe] = useState<DelegateProbe | null>(null);
  const [err, setErr] = useState("");

  const current = useMemo(() => spec.find((s) => s.type === type), [spec, type]);
  // The canonical ACP coding-agent catalog (single source — /api/acp-agents).
  const acpAgents = useQuery({ ...acpAgentsQuery(), enabled: type === "acp" });

  const hasEnvField = (current?.fields ?? []).some((f) => f.kind === "envmap");

  function buildEntry(): Record<string, unknown> {
    const entry: Record<string, unknown> = { name, type, description };
    for (const f of current?.fields ?? []) {
      if (f.kind === "envmap") continue; // serialized from form-level env state below
      const v = coerce(f, vals[f.key]);
      // skip blank secrets on edit so we don't overwrite a stored one with ""
      if (f.kind === "secret" && (v === "" || v == null)) continue;
      if (v === "" || v == null) continue;
      setDotted(entry, f.key, v);
    }
    if (hasEnvField) {
      // env: {NAME: value} for every keyed row; a value verbatim (blank secret row =
      // keep stored). env_secret lists the toggled keys — the backend routes those (+
      // any secret-named key) to secrets.yaml and keeps an empty reference in config.
      const env: Record<string, string> = {};
      const envSecret: string[] = [];
      for (const row of envRows) {
        const k = row.key.trim();
        if (!k) continue;
        env[k] = row.value;
        if (row.secret) envSecret.push(k);
      }
      if (Object.keys(env).length) {
        entry.env = env;
        if (envSecret.length) entry.env_secret = envSecret;
      }
      const removeList = envRemove.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
      if (removeList.length) entry.env_remove = removeList;
    }
    return entry;
  }

  const test = useMutation({
    mutationFn: () => api.testDelegate(buildEntry()),
    onSuccess: (p) => { setProbe(p); setErr(""); },
    onError: (e) => setErr(errMsg(e)),
  });

  const save = useMutation({
    mutationFn: () => (editing ? api.updateDelegate(name, buildEntry()) : api.createDelegate(buildEntry())),
    onSuccess: (r) => onSaved(r.message || (editing ? "updated" : "created")),
    onError: (e) => setErr(errMsg(e)),
  });

  return (
    <div className="delegate-form">
      {!editing ? (
        <RadioCardGroup
          name="delegate-type"
          min="160px"
          value={type}
          onValueChange={(v) => { setType(v); setProbe(null); setPreset(""); }}
        >
          {spec.map((s) => (
            <RadioCard key={s.type} value={s.type} title={s.label} blurb={s.blurb} />
          ))}
        </RadioCardGroup>
      ) : null}

      <label className="field">
        <span>Name</span>
        <Input value={name} disabled={editing} onChange={(e) => setName(e.target.value)} placeholder="e.g. opus" />
      </label>
      <label className="field">
        <span>Description</span>
        <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What it's for (the model reads this to pick it)." />
      </label>

      {type === "acp" && (acpAgents.data?.agents?.length ?? 0) > 0 ? (
        <label className="field">
          <span>Coding agent</span>
          <DropdownSelect
            id="acp-preset"
            value={preset}
            onValueChange={(id) => {
              setPreset(id);
              const a = acpAgents.data?.agents.find((x) => x.id === id);
              if (a) setVals((m) => ({ ...m, command: a.command, args: a.args.join(" ") }));
            }}
            options={[
              { value: "", label: "Custom / pick a preset…" },
              ...(acpAgents.data?.agents.map((a) => ({ value: a.id, label: a.label })) ?? []),
            ]}
          />
          <small className="delegate-field-help">
            Pre-fills the launch fields below for a known coding agent. Claude Code needs the
            adapter (<code>npm i -g @agentclientprotocol/claude-agent-acp</code>).
          </small>
        </label>
      ) : null}

      {(current?.fields ?? []).map((f) =>
        f.kind === "envmap" ? (
          <EnvEditor
            key={f.key}
            field={f}
            rows={envRows}
            setRows={setEnvRows}
            envRemove={envRemove}
            setEnvRemove={setEnvRemove}
          />
        ) : (
          <DelegateField
            key={f.key}
            field={f}
            value={vals[f.key] ?? ""}
            hasStoredSecret={editing && f.kind === "secret" && Boolean(initial?.has_secret)}
            onChange={(v) => setVals((m) => ({ ...m, [f.key]: v }))}
          />
        ),
      )}

      {probe ? <p className="settings-inline-status">{probeLine(probe)}</p> : null}
      {err ? <p className="settings-status">{err}</p> : null}

      <div className="settings-group-actions">
        <Button type="button" onClick={() => test.mutate()} loading={test.isPending}>
          {test.isPending ? null : <Plug size={15} />} Test
        </Button>
        <Button type="button" onClick={onClose}>Cancel</Button>
        <Button variant="primary" type="button" onClick={() => save.mutate()} loading={save.isPending} disabled={!name.trim()}>
          Save
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
      <DropdownSelect
        id={`del-${field.key}`}
        value={value}
        onValueChange={onChange}
        options={field.options.map((o) => ({ value: o, label: o || "(none)" }))}
      />
    );
  } else if (field.kind === "textarea") {
    control = <Textarea rows={3} placeholder={field.placeholder} {...common} />;
  } else if (field.kind === "secret") {
    control = (
      <SecretInput
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

// The per-delegate env editor (#2114): key/value rows with a per-row secret toggle,
// plus the env_remove list. Rendered in place of a plain field for the `envmap` kind.
function EnvEditor({
  field,
  rows,
  setRows,
  envRemove,
  setEnvRemove,
}: {
  field: DelegateFieldSpec;
  rows: EnvRow[];
  setRows: React.Dispatch<React.SetStateAction<EnvRow[]>>;
  envRemove: string;
  setEnvRemove: (v: string) => void;
}) {
  const patch = (i: number, p: Partial<EnvRow>) => setRows((r) => r.map((row, j) => (j === i ? { ...row, ...p } : row)));
  const addRow = () => setRows((r) => [...r, { key: "", value: "", secret: false }]);
  const delRow = (i: number) => setRows((r) => r.filter((_, j) => j !== i));
  return (
    <div className="field delegate-envmap">
      <span>{field.label}</span>
      <div className="delegate-env-rows">
        {rows.map((row, i) => (
          <div className="delegate-env-row" key={i}>
            <Input
              aria-label="env name"
              placeholder="NAME"
              value={row.key}
              onChange={(e) => patch(i, { key: e.target.value })}
            />
            {row.secret ? (
              <SecretInput
                aria-label="env value"
                autoComplete="new-password"
                placeholder="•••••••• (secret — blank keeps stored)"
                value={row.value}
                onChange={(e) => patch(i, { value: e.target.value })}
              />
            ) : (
              <Input
                aria-label="env value"
                placeholder="value"
                value={row.value}
                onChange={(e) => patch(i, { value: e.target.value })}
              />
            )}
            <Button
              icon
              variant={row.secret ? "primary" : "ghost"}
              type="button"
              title={row.secret ? "Secret — stored in secrets.yaml" : "Store as secret"}
              aria-pressed={row.secret}
              onClick={() => patch(i, { secret: !row.secret })}
            >
              <Lock size={14} />
            </Button>
            <Button icon variant="ghost" type="button" title="Remove variable" onClick={() => delRow(i)}>
              <Trash2 size={14} />
            </Button>
          </div>
        ))}
      </div>
      <div className="delegate-env-add">
        <Button icon variant="ghost" type="button" title="Add variable" onClick={addRow}>
          <Plus size={14} />
        </Button>
        <span className="delegate-field-help">Add variable</span>
      </div>
      {field.help ? <small className="delegate-field-help">{field.help}</small> : null}
      <label className="field delegate-env-remove">
        <span>Remove from inherited env</span>
        <Textarea
          rows={2}
          placeholder="PROTOAGENT_, A2A_AUTH_TOKEN"
          value={envRemove}
          onChange={(e) => setEnvRemove(e.target.value)}
        />
        <small className="delegate-field-help">
          Host env var names stripped from the child <em>before</em> the additions above merge in
          (remove-then-add). Comma-separated or one per line. A trailing underscore is a prefix
          match — <code>PROTOAGENT_</code> strips every <code>PROTOAGENT_*</code> var.
        </small>
      </label>
    </div>
  );
}

function seed(initial: DelegateView | null, spec: DelegateTypeSpec[]): Record<string, string> {
  const out: Record<string, string> = {};
  if (!initial) return out;
  const t = spec.find((s) => s.type === initial.type);
  for (const f of t?.fields ?? []) {
    if (f.kind === "envmap") continue; // env editor seeds from form-level state (seedEnvRows)
    const v = getDotted(initial, f.key);
    if (f.kind === "args" && Array.isArray(v)) out[f.key] = v.join(" ");
    else if (f.kind === "secret") out[f.key] = ""; // redacted; blank = keep stored
    else if (v != null) out[f.key] = String(v);
  }
  return out;
}
