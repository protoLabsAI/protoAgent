import { Alert } from "@protolabsai/ui/data";
import { Input, Select, Textarea } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { QueryErrorResetBoundary, useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { ExternalLink, Link2, Loader2, RotateCcw, Save, ShieldCheck } from "lucide-react";

import { Suspense, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { api } from "../lib/api";
import { queryKeys, settingsSchemaQuery } from "../lib/queries";
import type { SettingsField, SettingsGroup } from "../lib/types";

// Drop-in full-panel wrapper (section + Suspense + ErrorBoundary) so any surface can
// embed a category's settings as a standalone panel — Agent, Knowledge, central Settings.
export function SettingsCategoryPanel(props: { category: string; title?: string; emptyHint?: string; footer?: ReactNode }) {
  return (
    <section className="panel stage-panel settings-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="settings" />}>
            <Suspense fallback={<PanelSkeleton label="Loading settings…" />}>
              <SettingsCategory {...props} />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}

// Host / box-shared defaults view (ADR 0047) — the host-scoped fields across ALL
// categories, editable, saving to the host layer. Surfaced as its own Settings tab.
// TODO(ADR 0047 §7): gate to the host console (slug=host); for now it renders for any
// focused agent, clearly labeled "box-shared", so the slice isn't blocked on gating.
export function HostDefaultsPanel({ categories = ["Agent", "System"] }: { categories?: string[] }) {
  return (
    <section className="panel stage-panel settings-panel">
      {categories.map((category) => (
        <QueryErrorResetBoundary key={category}>
          {({ reset }) => (
            <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="host defaults" />}>
              <Suspense fallback={<PanelSkeleton label="Loading host defaults…" />}>
                <SettingsCategory
                  category={category}
                  title={`Host defaults · ${category}`}
                  emptyHint={`No box-shared defaults under ${category}.`}
                  hostLayer
                />
              </Suspense>
            </ErrorBoundary>
          )}
        </QueryErrorResetBoundary>
      ))}
    </section>
  );
}

// Setup walkthroughs live in the template's docs (forks don't ship their own site).
const DISCORD_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/discord#bot-setup";
const GOOGLE_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/google#oauth-client";

// One category's settings — the field groups tagged with `category`, rendered with
// their own dirty-tracking, Save-&-apply, and per-group Test buttons. Extracted from
// the old monolithic SettingsSurface so settings can live in their home view (Agent,
// Knowledge, …) instead of one bucket. Each instance owns its own dirty state, so you
// save the settings where they live.

export function SettingsCategory({
  category,
  title = "Settings",
  emptyHint,
  footer,
  // ADR 0047 host-defaults view: when true this renders ONLY the host-scoped
  // (box-shared) fields and a Save writes to the host layer instead of the agent
  // leaf. The default (false) is the per-agent Settings — every field, with the
  // inherited-vs-overridden badge + reset-to-inherited affordance.
  hostLayer = false,
}: {
  category: string;
  title?: string;
  emptyHint?: string;
  footer?: ReactNode;
  hostLayer?: boolean;
}) {
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery(settingsSchemaQuery());
  const groups = useMemo(() => {
    const byCategory = data.groups.filter((g) => (g.category || "Plugins") === category);
    if (!hostLayer) return byCategory;
    // Host-defaults view: keep only the host-scoped fields, dropping now-empty groups.
    return byCategory
      .map((g) => ({ ...g, fields: g.fields.filter((f) => f.scope === "host") }))
      .filter((g) => g.fields.length);
  }, [data.groups, category, hostLayer]);
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [status, setStatus] = useState("");
  const dirtyKeys = Object.keys(dirty);

  const hasModel = groups.some((g) => g.fields.some((f) => f.key === "model.name"));
  const hasDiscord = groups.some((g) => g.section === "Discord");
  const hasGoogle = groups.some((g) => g.section === "Google");

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

  const save = useMutation({
    mutationFn: () => api.saveSettings(dirty, hostLayer ? "host" : "agent"),
    onMutate: () => setStatus("saving…"),
    onSuccess: (r) => {
      if (!r.ok) { setStatus(`save failed: ${r.messages.join(" · ")}`); return; }
      const restartNote = r.restart_required.length ? ` — restart required for: ${r.restart_required.join(", ")}` : "";
      setStatus(`${r.messages.join(" · ")}${restartNote}`);
      setDirty({});
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
    onError: (e) => setStatus(`save failed: ${e instanceof Error ? e.message : String(e)}`),
  });

  // ADR 0047 reset-to-inherited — pop one (or more) overridden keys from the agent
  // leaf so each falls back to the Host/App layer. Invalidate the schema on success
  // so the badges + values re-resolve to the inherited source (consistent with save).
  const reset = useMutation({
    mutationFn: (keys: string[]) => api.resetSettings(keys),
    onMutate: () => setStatus("resetting to inherited…"),
    onSuccess: (r, keys) => {
      if (!r.ok) { setStatus(`reset failed: ${r.messages.join(" · ")}`); return; }
      setStatus(r.messages.join(" · "));
      // Drop any pending edit on the reset keys — the inherited value is now authoritative.
      setDirty((d) => { const next = { ...d }; for (const k of keys) delete next[k]; return next; });
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
    onError: (e) => setStatus(`reset failed: ${e instanceof Error ? e.message : String(e)}`),
  });

  const asStr = (v: unknown) => (typeof v === "string" ? v : "");
  const testConn = useMutation({
    mutationFn: () => api.testModel(asStr(dirty["model.api_base"]), asStr(dirty["model.api_key"]), asStr(dirty["model.name"])),
    onMutate: () => setStatus("testing connection…"),
    onSuccess: (r) => setStatus(r.ok ? "connection OK — the model responded." : `connection failed — ${r.error || "no response"}`),
    onError: (e) => setStatus(`connection test failed: ${e instanceof Error ? e.message : String(e)}`),
  });

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
    onMutate: () => setStatus("testing connection…"),
    onSuccess: (r) => setStatus(r.ok ? `connection OK${r.identity ? ` — ${r.identity}` : ""}` : `connection failed — ${r.error || "no response"}`),
    onError: (e) => setStatus(`connection test failed: ${e instanceof Error ? e.message : String(e)}`),
    onSettled: () => setTestingSection(null),
  });

  const testDiscord = useMutation({
    mutationFn: () => api.testDiscord(asStr(dirty["discord.bot_token"])),
    onMutate: () => setStatus("testing Discord…"),
    onSuccess: (r) => setStatus(r.ok ? `Discord OK — connected as ${r.bot_user || "your bot"}.` : `Discord connection failed — ${r.error || "check the token"}`),
    onError: (e) => setStatus(`Discord test failed: ${e instanceof Error ? e.message : String(e)}`),
  });

  const googleStatus = useQuery({ queryKey: ["google-status"], queryFn: () => api.googleStatus(), enabled: hasGoogle });
  const googleConnect = useMutation({
    mutationFn: () => api.googleConnect(),
    onMutate: () => setStatus("opening Google consent in your browser…"),
    onSuccess: (r) => {
      setStatus(r.ok ? `Google connected${r.email ? ` as ${r.email}` : ""}.` : `Google connect failed — ${r.error || "try again"}`);
      void googleStatus.refetch();
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
    onError: (e) => setStatus(`Google connect failed: ${e instanceof Error ? e.message : String(e)}`),
  });
  const dirtyGoogleClient = "google.client_id" in dirty || "google.client_secret" in dirty;

  const discard = () => { setDirty({}); setStatus(""); };

  return (
    <>
      <PanelHeader
        title={title}
        kicker={
          dirtyKeys.length
            ? `${dirtyKeys.length} unsaved change${dirtyKeys.length === 1 ? "" : "s"}`
            : hostLayer
              ? "saves to the box-shared host defaults"
              : runtimeField
                ? `runtime: ${acpAgent ? `${acpAgent} (ACP)` : "native"}`
                : "applies on save"
        }
        actions={
          <>
            {hasModel ? (
              <Button type="button" onClick={() => testConn.mutate()} disabled={testConn.isPending || save.isPending}>
                {testConn.isPending ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                Test connection
              </Button>
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
        {hostLayer ? (
          <Alert status="info" className="settings-banner">
            <strong>Host / box-shared defaults</strong> (ADR 0047) — edits here write to the
            box's <code>host-config.yaml</code> and become the inherited default for every agent
            on this box that hasn't set its own value. Per-agent overrides win.
            {/* TODO(ADR 0047 §7): gate this view to the host console (slug=host). For now it
                renders for any focused agent — clearly labeled as box-shared — so the slice
                isn't blocked on host-console gating. */}
          </Alert>
        ) : null}
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
        {status ? <p className="settings-status">{status}</p> : null}
        {!groups.length && !footer ? <p className="muted">{emptyHint || "Nothing to configure here."}</p> : null}

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
                // The host-defaults view edits the host layer directly — every field IS the
                // shared default there, so the per-agent inheritance badge would be noise. The
                // per-agent view shows the badge + reset affordance.
                showInheritance={!hostLayer}
                onReset={() => reset.mutate([field.key])}
                resetting={reset.isPending}
              />
            ))}
            {hasDiscord && group.section === "Discord" ? (
              <div className="settings-group-actions">
                <Button type="button" onClick={() => testDiscord.mutate()} disabled={testDiscord.isPending || save.isPending}>
                  {testDiscord.isPending ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                  Test connection
                </Button>
                <a className="settings-help-link" href={DISCORD_GUIDE_URL} target="_blank" rel="noreferrer">
                  How to create a bot <ExternalLink size={13} />
                </a>
              </div>
            ) : null}
            {hasGoogle && group.section === "Google" ? (
              <div className="settings-group-actions">
                <Button
                 
                  type="button"
                  onClick={() => googleConnect.mutate()}
                  disabled={googleConnect.isPending || save.isPending || dirtyGoogleClient}
                  title={dirtyGoogleClient ? "Save the client ID + secret first" : undefined}
                >
                  {googleConnect.isPending ? <Loader2 className="spin" size={15} /> : <Link2 size={15} />}
                  {googleStatus.data?.connected ? "Reconnect Google" : "Connect Google"}
                </Button>
                <span className="settings-inline-status">
                  {googleStatus.data?.connected
                    ? `Connected${googleStatus.data.email ? ` as ${googleStatus.data.email}` : ""}`
                    : dirtyGoogleClient
                      ? "Save the client ID + secret, then connect"
                      : "Not connected"}
                </span>
                <a className="settings-help-link" href={GOOGLE_GUIDE_URL} target="_blank" rel="noreferrer">
                  Get an OAuth client <ExternalLink size={13} />
                </a>
              </div>
            ) : null}
            {group.test ? (
              <div className="settings-group-actions">
                <Button
                 
                  type="button"
                  onClick={() => { setTestingSection(group.section); testGroup.mutate({ endpoint: group.test!.endpoint, fields: groupFields(group) }); }}
                  disabled={(testGroup.isPending && testingSection === group.section) || save.isPending}
                >
                  {testGroup.isPending && testingSection === group.section ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                  Test connection
                </Button>
              </div>
            ) : null}
          </section>
        ))}
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
function inheritance(field: SettingsField): { label: string; status: "neutral" | "info" | "warning"; overridden: boolean } | null {
  if (field.source === "host") return { label: "inherited from Host", status: "neutral", overridden: false };
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
  onReset,
  resetting = false,
}: {
  field: SettingsField;
  value: unknown;
  dirty: boolean;
  onChange: (value: unknown) => void;
  showInheritance?: boolean;
  onReset?: () => void;
  resetting?: boolean;
}) {
  const inherit = showInheritance ? inheritance(field) : null;
  return (
    <div className={`setting-row${dirty ? " dirty" : ""}`} data-key={field.key}>
      <div className="setting-meta">
        <label className="setting-label" htmlFor={`set-${field.key}`}>
          {field.label}
          {field.restart ? <span className="setting-restart">restart</span> : null}
        </label>
        {field.description ? <p className="setting-desc">{field.description}</p> : null}
        {inherit ? (
          <p className="setting-inheritance">
            <Badge status={inherit.status}>{inherit.label}</Badge>
            {inherit.overridden && onReset ? (
              <Button variant="ghost" size="sm" type="button" onClick={onReset} disabled={resetting}>
                {resetting ? <Loader2 className="spin" size={13} /> : <RotateCcw size={13} />}
                Reset to inherited
              </Button>
            ) : null}
          </p>
        ) : null}
      </div>
      <div className="setting-control">
        <SettingInput field={field} value={value} onChange={onChange} />
      </div>
    </div>
  );
}

function SettingInput({ field, value, onChange }: { field: SettingsField; value: unknown; onChange: (value: unknown) => void }) {
  const id = `set-${field.key}`;

  if (field.type === "bool") {
    return (
      <label className="setting-toggle">
        <input id={id} type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        <span>{value ? "on" : "off"}</span>
      </label>
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
      <Select id={id} className="setting-input" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
        {field.options.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
      </Select>
    );
  }
  if (field.type === "string_list") {
    const text = Array.isArray(value) ? value.join("\n") : "";
    return (
      <Textarea
        id={id}
        className="setting-input setting-textarea"
        rows={3}
        value={text}
        placeholder="one per line"
        onChange={(e) => onChange(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))}
      />
    );
  }
  if (field.type === "secret") {
    return (
      <Input
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
