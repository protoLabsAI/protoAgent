import "./settings.css";

import { Alert } from "@protolabsai/ui/data";
import { Combobox, DropdownSelect, Input, SecretInput, Switch, Textarea } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Boxes, RotateCcw, Save } from "lucide-react";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { Accordion, AccordionItem, PanelHeader } from "@protolabsai/ui/navigation";
import { useToast } from "@protolabsai/ui/overlays";
import { StagePanel } from "../app/ErrorBoundary";
import { HelpLink, TestConnectionButton } from "../app/ui-kit";
import { api, isHostConsole } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, settingsSchemaQuery } from "../lib/queries";
import type { SettingsField, SettingsGroup } from "../lib/types";
import { fieldVisible } from "./visibility";

// Drop-in full-panel wrapper (section + Suspense + ErrorBoundary) so any surface can
// embed a category's settings as a standalone panel — Agent, Knowledge, central Settings.
export function SettingsCategoryPanel(props: { category: string; title?: string; emptyHint?: string; footer?: ReactNode }) {
  return (
    <StagePanel label="settings" className="settings-panel">
      <SettingsCategory {...props} />
    </StagePanel>
  );
}

// One category's settings — the field groups tagged with `category`, rendered with
// their own dirty-tracking, Save-&-apply, and per-group Test buttons. Extracted from
// the old monolithic SettingsSurface so settings can live in their home view (Agent,
// Knowledge, …) instead of one bucket. Each instance owns its own dirty state, so you
// save the settings where they live.

export function SettingsCategory({
  category,
  // Host-defaults view aggregates several categories into ONE panel; when set,
  // `categories` supersedes `category` — the fields across all of them render
  // together under a single Save bar (ADR 0047).
  categories,
  title = "Settings",
  emptyHint,
  footer,
  // ADR 0059 — when set, render ONLY this plugin's group (its config folded into the
  // plugin's row in the Plugins surface). Pairs with category="Plugins".
  pluginId,
}: {
  category: string;
  categories?: string[];
  title?: string;
  emptyHint?: string;
  footer?: ReactNode;
  pluginId?: string;
}) {
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery(settingsSchemaQuery());
  const groups = useMemo(() => {
    // One category, or several aggregated into one panel (the `categories` prop).
    const inScope = (g: SettingsGroup) =>
      categories ? categories.includes(g.category || "Plugins") : (g.category || "Plugins") === category;
    let selected = data.groups.filter(inScope);
    if (pluginId) selected = selected.filter((g) => g.plugin_id === pluginId);  // one plugin's group (ADR 0059)
    return selected;
  }, [data.groups, category, categories, pluginId]);
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const dirtyKeys = Object.keys(dirty);
  // Action feedback is a TOAST, not an inline line — transient success/error belongs in the
  // global toaster (the in-progress state is already on each button's pending spinner).
  const toast = useToast();

  // #963 — conditional field visibility. The live value of every in-scope field
  // (the dirty edit if any, else the saved value), so a `depends_on` predicate is
  // reactive to what's on the form right now, not just what was last saved.
  const currentValues = useMemo(() => {
    const m = new Map<string, unknown>();
    for (const g of groups) for (const f of g.fields) m.set(f.key, f.key in dirty ? dirty[f.key] : f.value);
    return m;
  }, [groups, dirty]);
  const isVisible = (field: SettingsField): boolean => fieldVisible(field, (k) => currentValues.get(k));

  const hasModel = groups.some((g) => g.fields.some((f) => f.key === "model.name"));

  // Active agent runtime (ADR 0033) — for the banner + header badge when this category
  // carries the selector (the Agent settings). Reflects the pending (dirty) choice live.
  const runtimeField = groups.flatMap((g) => g.fields).find((f) => f.key === "agent_runtime");
  const activeRuntime = runtimeField
    ? String((dirty["agent_runtime"] ?? runtimeField.value) ?? "native")
    : null;
  const acpAgent = activeRuntime && activeRuntime.startsWith("acp:") ? activeRuntime.slice(4) : null;

  const pendingRestart = useMemo(() => {
    const labels: string[] = [];
    for (const g of groups) for (const f of g.fields) if (f.restart && f.key in dirty) labels.push(f.label);
    return labels;
  }, [groups, dirty]);

  // Which layer a Save lands in (ADR 0047). On the HOST console a host-scoped field sets the
  // box default (host layer) while an agent-scoped field is the host agent's own (agent leaf)
  // — so split the write by scope. On a fleet member everything overrides into that agent's leaf.
  const onHost = isHostConsole();
  const save = useMutation({
    mutationFn: async () => {
      if (!onHost) return api.saveSettings(dirty, "agent");
      const scopeOf = (k: string) =>
        groups.flatMap((g) => g.fields).find((f) => f.key === k)?.scope ?? "agent";
      const hostU: Record<string, unknown> = {};
      const agentU: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(dirty)) (scopeOf(k) === "host" ? hostU : agentU)[k] = v;
      const rs = await Promise.all([
        ...(Object.keys(hostU).length ? [api.saveSettings(hostU, "host")] : []),
        ...(Object.keys(agentU).length ? [api.saveSettings(agentU, "agent")] : []),
      ]);
      return {
        ok: rs.every((r) => r.ok),
        messages: rs.flatMap((r) => r.messages),
        restart_required: rs.flatMap((r) => r.restart_required),
      };
    },
    onSuccess: (r) => {
      if (!r.ok) { toast({ tone: "error", title: "Save failed", message: r.messages.join(" · ") }); return; }
      const restartNote = r.restart_required.length ? `Restart required for: ${r.restart_required.join(", ")}` : "";
      toast({ tone: "success", title: "Settings saved", message: restartNote || r.messages.join(" · ") || "Applied." });
      setDirty({});
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
    onError: (e) => toast({ tone: "error", title: "Save failed", message: errMsg(e) }),
  });

  // ADR 0047 reset-to-inherited — pop one (or more) overridden keys from the agent
  // leaf so each falls back to the Host/App layer. Invalidate the schema on success
  // so the badges + values re-resolve to the inherited source (consistent with save).
  const reset = useMutation({
    mutationFn: (keys: string[]) => api.resetSettings(keys),
    onSuccess: (r, keys) => {
      if (!r.ok) { toast({ tone: "error", title: "Reset failed", message: r.messages.join(" · ") }); return; }
      toast({ tone: "success", title: "Reset to inherited", message: r.messages.join(" · ") || "Back to the inherited value." });
      // Drop any pending edit on the reset keys — the inherited value is now authoritative.
      setDirty((d) => { const next = { ...d }; for (const k of keys) delete next[k]; return next; });
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
    onError: (e) => toast({ tone: "error", title: "Reset failed", message: errMsg(e) }),
  });

  const asStr = (v: unknown) => (typeof v === "string" ? v : "");
  const testConn = useMutation({
    mutationFn: () => api.testModel(asStr(dirty["model.api_base"]), asStr(dirty["model.api_key"]), asStr(dirty["model.name"])),
    onSuccess: (r) =>
      r.ok
        ? toast({ tone: "success", title: "Connection OK", message: "The model responded." })
        : toast({ tone: "error", title: "Connection failed", message: r.error || "no response" }),
    onError: (e) => toast({ tone: "error", title: "Connection test failed", message: errMsg(e) }),
  });

  // "Get models" (#1386): probe the gateway named on the FORM — its api_base/key, which may be
  // a NEW provider you haven't saved yet — for its model list, so you can pick a valid model
  // BEFORE saving and testing (the saved dropdown would otherwise be stuck on the old gateway's
  // models → a dead-end). The result is merged into every model-backed dropdown below.
  const [gatewayModels, setGatewayModels] = useState<string[] | null>(null);
  const apiBaseField = useMemo(
    () => groups.flatMap((g) => g.fields).find((f) => f.key === "model.api_base"),
    [groups],
  );
  const getModels = useMutation({
    // api_base: the form edit, else the saved value. api_key: the form edit, else blank — the
    // server falls back to the saved (secret) key, which never leaves localStorage as plaintext.
    mutationFn: () => api.models(asStr(dirty["model.api_base"]) || asStr(apiBaseField?.value), asStr(dirty["model.api_key"])),
    onSuccess: (r) => {
      if (r.error) { toast({ tone: "error", title: "Couldn't fetch models", message: r.error }); return; }
      setGatewayModels(r.models);
      toast(
        r.models.length
          ? { tone: "success", title: `Found ${r.models.length} model${r.models.length === 1 ? "" : "s"}`, message: "Pick one in Primary model, then Test connection." }
          : { tone: "info", title: "No models", message: "The gateway returned no models." },
      );
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't fetch models", message: errMsg(e) }),
  });
  // Merge the freshly-probed models into a model-backed field's options (new gateway's models
  // first, then whatever was saved), so the dropdown isn't stuck on the old provider's list.
  const withGatewayModels = (field: SettingsField): SettingsField =>
    gatewayModels && (field.options_source === "models" || field.options_source === "models+acp")
      ? { ...field, options: [...new Set([...gatewayModels, ...field.options])] }
      : field;

  // Generic per-group "Test connection" (ADR 0029).
  const [testingSection, setTestingSection] = useState<string | null>(null);
  const groupFields = (group: SettingsGroup): Record<string, unknown> => {
    const out: Record<string, unknown> = {};
    for (const f of group.fields) {
      const short = f.key.split(".").pop() as string;
      if (f.key in dirty) out[short] = dirty[f.key];
      else if (f.type !== "secret") out[short] = f.value;
    }
    return out;
  };
  const testGroup = useMutation({
    mutationFn: (vars: { endpoint: string; fields: Record<string, unknown> }) => api.testConfig(vars.endpoint, vars.fields),
    onSuccess: (r) =>
      r.ok
        ? toast({ tone: "success", title: "Connection OK", message: r.identity || "Connected." })
        : toast({ tone: "error", title: "Connection failed", message: r.error || "no response" }),
    onError: (e) => toast({ tone: "error", title: "Connection test failed", message: errMsg(e) }),
    onSettled: () => setTestingSection(null),
  });

  const discard = () => setDirty({});

  // The fields + Test/Connect for one group — rendered inside an AccordionItem in the
  // full Settings view, or flat (no accordion) when folded into a plugin's row (ADR 0059).
  const renderGroupBody = (group: SettingsGroup) => (
    <>
      {group.fields.filter(isVisible).map((field) => (
        <SettingRow
          key={field.key}
          field={withGatewayModels(field)}
          dirty={field.key in dirty}
          value={field.key in dirty ? dirty[field.key] : field.value}
          showInheritance
          onHost={onHost}
          onChange={(v) => setDirty((d) => ({ ...d, [field.key]: v }))}
          onReset={() => reset.mutate([field.key])}
          resetting={reset.isPending}
        />
      ))}
      {/* Generic, data-driven group actions (ADR 0059) — a Test button from the
          manifest's `test: true` and a setup-guide link from `guide_url`. No
          per-plugin frontend. */}
      {group.test || group.guide_url ? (
        <div className="settings-group-actions">
          {group.test ? (
            <TestConnectionButton
              onClick={() => { setTestingSection(group.section); testGroup.mutate({ endpoint: group.test!.endpoint, fields: groupFields(group) }); }}
              pending={testGroup.isPending && testingSection === group.section}
              disabled={save.isPending}
            />
          ) : null}
          {group.guide_url ? <HelpLink href={group.guide_url}>Setup guide</HelpLink> : null}
        </div>
      ) : null}
    </>
  );

  return (
    <>
      <PanelHeader
        title={title}
        kicker={
          dirtyKeys.length
            ? `${dirtyKeys.length} unsaved change${dirtyKeys.length === 1 ? "" : "s"}`
            : runtimeField
              ? `runtime: ${acpAgent ? `${acpAgent} (ACP)` : "native"}`
              : "applies on save"
        }
        actions={
          <>
            {hasModel ? (
              <>
                {/* #1386 — pull the form gateway's model list into the Primary model dropdown, so
                    switching provider/key isn't a dead-end (the saved list is stale). */}
                <Button type="button" onClick={() => getModels.mutate()} loading={getModels.isPending} disabled={save.isPending}>
                  {getModels.isPending ? null : <Boxes size={15} />} Get models
                </Button>
                <TestConnectionButton onClick={() => testConn.mutate()} pending={testConn.isPending} disabled={save.isPending} />
              </>
            ) : null}
            {/* Pilot of the protoLabs design system (ADR 0037 D7) — the real @protolabsai/ui Button. */}
            <Button type="button" onClick={discard} disabled={save.isPending || !dirtyKeys.length}>
              <RotateCcw size={15} /> Discard
            </Button>
            <Button variant="primary" type="button" onClick={() => save.mutate()} disabled={save.isPending || !dirtyKeys.length}>
              <Save size={16} /> Save &amp; apply
            </Button>
          </>
        }
      />
      <div className="stage-body">
        {acpAgent ? (
          <Alert status="info" className="settings-banner">
            Running on <strong>{acpAgent}</strong> (ACP) — it drives each turn with its own tools.
            The model settings below power protoAgent's own calls (compaction, goal checks); with no
            gateway key configured, those run on {acpAgent} too.
          </Alert>
        ) : null}
        {pendingRestart.length ? (
          <Alert status="warning" className="settings-banner">
            Needs a restart to take effect: {pendingRestart.join(", ")}
          </Alert>
        ) : null}
        {!groups.length && !footer ? <p className="muted">{emptyHint || "Nothing to configure here."}</p> : null}

        {/* Each field group is a collapsible accordion (DS 0.29) so a dense category
            (the Workspace home's panels run long) can be tidied to the few sections
            you're editing. Collapsed by default — the operator expands groups as
            needed. A dirty-count badge rides the title so a collapsed group still
            announces it has unsaved edits. */}
        {/* Folded into a plugin's row (ADR 0059): render the single group's fields
            FLAT — the row's Configure toggle is the disclosure, so a nested accordion
            would be a second click. The full Settings view keeps the collapsible groups. */}
        {pluginId ? (
          <div className="settings-groups">
            {groups.map((group) => (
              <div className="settings-flat-group" key={group.section}>{renderGroupBody(group)}</div>
            ))}
          </div>
        ) : (
          <Accordion className="settings-groups">
            {groups.map((group, i) => {
              const groupDirty = group.fields.filter(isVisible).reduce((n, f) => n + (f.key in dirty ? 1 : 0), 0);
              return (
                <AccordionItem
                  key={group.section}
                  // Open the first group by default so a panel never lands fully collapsed
                  // (the operator sees content immediately; the rest expand on demand).
                  defaultOpen={i === 0}
                  title={
                    <span className="settings-group-head">
                      {group.section}
                      {groupDirty ? <Badge status="warning">{groupDirty} unsaved</Badge> : null}
                    </span>
                  }
                >
                  {renderGroupBody(group)}
                </AccordionItem>
              );
            })}
          </Accordion>
        )}
        {footer}
      </div>
    </>
  );
}

// The inheritance state of a field, derived from ADR 0047 `source`+`scope`:
//   source=="host"                    → inherited from Host
//   source=="default"                 → inherited from default (App dataclass)
//   source=="agent" && scope=="host"  → overridden here (offer reset-to-inherited)
//   source=="agent" && scope=="agent" → a plain agent setting (no badge)
function inheritance(field: SettingsField, onHost: boolean): { label: string; status: "neutral" | "info" | "warning"; overridden: boolean } | null {
  if (onHost) {
    // On the host console you ARE the box: a host-scoped field is the shared default every
    // agent inherits (not "inherited from" anything); agent-scoped fields are the host's own.
    // But if the agent leaf ALSO sets it (source=="agent"), the leaf WINS at runtime (ADR 0047)
    // and silently shadows the box default — surface that as a warning + reset (issue #1459).
    if (field.scope === "host")
      return field.source === "agent"
        ? { label: "overridden by agent config", status: "warning", overridden: true }
        : { label: "box default", status: "info", overridden: false };
    return null;
  }
  // A fleet member (non-host): the ADR 0047 inheritance view.
  if (field.source === "host") return { label: "inherited from host", status: "neutral", overridden: false };
  if (field.source === "default") return { label: "inherited from default", status: "neutral", overridden: false };
  if (field.source === "agent" && field.scope === "host") return { label: "overridden here", status: "warning", overridden: true };
  return null; // source=="agent" && scope=="agent" — just an agent setting.
}

function SettingRow({
  field,
  value,
  dirty,
  onChange,
  showInheritance = true,
  onHost = false,
  onReset,
  resetting = false,
}: {
  field: SettingsField;
  value: unknown;
  dirty: boolean;
  onChange: (value: unknown) => void;
  showInheritance?: boolean;
  onHost?: boolean;
  onReset?: () => void;
  resetting?: boolean;
}) {
  const inherit = showInheritance ? inheritance(field, onHost) : null;
  return (
    <div className={`setting-row${dirty ? " dirty" : ""}`} data-key={field.key}>
      <div className="setting-meta">
        <label className="setting-label" htmlFor={`set-${field.key}`}>
          {field.label}
          {/* A configured secret never echoes its value, so without a glanceable
              indicator a saved key looks identical to an empty one ("did it save?").
              Mirror the Delegates panel's "secret set" pill. */}
          {field.type === "secret" && field.is_set ? <Badge status="success">set</Badge> : null}
          {field.restart ? <Badge status="warning">restart</Badge> : null}
        </label>
        {field.description ? <p className="setting-desc">{field.description}</p> : null}
        {inherit ? (
          <p className="setting-inheritance">
            <Badge status={inherit.status}>{inherit.label}</Badge>
            {inherit.overridden && onReset ? (
              <Button variant="ghost" size="sm" type="button" onClick={onReset} loading={resetting}>
                {resetting ? null : <RotateCcw size={13} />}
                Reset to inherited
              </Button>
            ) : null}
          </p>
        ) : null}
        {/* Editing a still-inherited box-shared field in the Workspace view writes a
            per-agent leaf override (not the box default) — make that effect explicit. */}
        {showInheritance && !onHost && dirty && field.scope === "host" && field.source !== "agent" ? (
          <p className="setting-override-note">Saving overrides the box default for this agent only.</p>
        ) : null}
        {/* Host console: this box default is shadowed by the agent leaf — the shown value is the
            effective (agent) value that wins, so editing the box default here has no runtime effect
            until the agent override is removed via Reset to inherited (issue #1459). */}
        {showInheritance && onHost && field.scope === "host" && field.source === "agent" ? (
          <p className="setting-override-note">
            The agent config overrides this box default — the agent value shown is what runs. Reset to use the box default.
          </p>
        ) : null}
      </div>
      <div className="setting-control">
        <SettingInput field={field} value={value} onChange={onChange} />
      </div>
    </div>
  );
}

// A free-form string list (repos, allowed dirs, …) edited as ONE text field. Items are
// separated by a comma OR a newline — both work, so you can paste "a, b, c" or one per
// line. It keeps its own raw text while focused so a separator isn't eaten mid-type (the
// old controlled `value={list.join(...)}` re-derived the text every keystroke, dropping
// the trailing separator via filter(Boolean) — which made adding a 2nd item impossible).
// Parsed → a clean string[] on every change; re-syncs from `value` on an external change
// (discard / reset / load).
// Split a string-list text field into clean items: comma OR newline separated, trimmed,
// empties dropped — so "a, b", "a\nb", and "a , , b\n" all yield ["a","b"].
export function parseStringList(text: string): string[] {
  return text.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
}

function StringListInput({ id, value, onChange }: { id: string; value: unknown; onChange: (v: unknown) => void }) {
  const items = Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  const [text, setText] = useState(items.join(", "));
  const lastParsed = useRef(items);
  useEffect(() => {
    // Adopt the parent value only when it changed to something we didn't just emit
    // (e.g. Discard reverted it) — otherwise leave the user's in-progress text alone.
    if (JSON.stringify(items) !== JSON.stringify(lastParsed.current)) {
      setText(items.join(", "));
      lastParsed.current = items;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <Textarea
      id={id}
      className="setting-input setting-textarea"
      rows={2}
      value={text}
      placeholder="comma-separated (e.g. owner/repo, owner/repo2)"
      onChange={(e) => {
        setText(e.target.value);
        const parsed = parseStringList(e.target.value);
        lastParsed.current = parsed;
        onChange(parsed);
      }}
    />
  );
}

export function SettingInput({ field, value, onChange }: { field: SettingsField; value: unknown; onChange: (value: unknown) => void }) {
  const id = `set-${field.key}`;

  if (field.type === "bool") {
    return (
      <Switch
        id={id}
        checked={Boolean(value)}
        onCheckedChange={onChange}
        label={value ? "on" : "off"}
      />
    );
  }
  if (field.type === "number") {
    return (
      <Input
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
      <DropdownSelect
        id={id}
        className="setting-input"
        value={String(value ?? "")}
        onValueChange={(v) => onChange(v)}
        options={field.options.map((opt) => ({ value: opt, label: opt }))}
      />
    );
  }
  // A string_list backed by gateway options (e.g. routing.fallback_models)
  // renders as a list of datalist comboboxes — one per value plus a trailing
  // blank row to add — so you pick models from the gateway (or type any alias),
  // ordered. Clearing a row removes it.
  if (field.type === "string_list" && field.options.length) {
    const items = Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
    const update = (i: number, v: string) => {
      const next = items.slice();
      if (v) next[i] = v;
      else next.splice(i, 1);
      onChange(next);
    };
    // A column of DS Comboboxes — one per value plus a trailing blank-to-add row;
    // each carries its own suggestion list (the gateway models), and clearing a row
    // removes it. Type any alias OR pick a suggestion.
    return (
      <div id={id} className="setting-list" style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
        {[...items, ""].map((item, i) => (
          <Combobox
            key={i}
            className="setting-input"
            options={field.options}
            value={item}
            placeholder={i === items.length ? "add a model…" : ""}
            onValueChange={(v) => update(i, v)}
          />
        ))}
      </div>
    );
  }
  if (field.type === "string_list") {
    return <StringListInput id={id} value={value} onChange={onChange} />;
  }
  if (field.type === "text") {
    // A scalar multiline string (#964) — a system prompt, template, or blurb. Renders
    // a textarea but casts/saves exactly like `string` (one value, no list semantics).
    return (
      <Textarea
        id={id}
        className="setting-input setting-textarea"
        rows={4}
        value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  if (field.type === "secret") {
    return (
      <SecretInput
        id={id}
        className="setting-input"
        autoComplete="new-password"
        value={typeof value === "string" ? value : ""}
        placeholder={field.is_set ? "•••••••• (set — leave blank to keep)" : "unset"}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  // A free-text string field that carries gateway-sourced options (e.g.
  // routing.aux_model, knowledge.transcribe_model) renders as a combobox: type
  // any alias OR pick from the gateway's models. Unlike `select`, blank/arbitrary
  // values stay valid (these fields aren't membership-checked), so a datalist of
  // suggestions is the right control.
  if (field.options.length) {
    return (
      <Combobox
        id={id}
        className="setting-input"
        options={field.options}
        placeholder="type or pick a model"
        value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)}
        onValueChange={onChange}
      />
    );
  }
  return (
    <Input
      id={id}
      className="setting-input"
      type="text"
      value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
