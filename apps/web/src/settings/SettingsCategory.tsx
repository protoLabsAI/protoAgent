import { QueryErrorResetBoundary, useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { AlertTriangle, Bot, ExternalLink, Link2, Loader2, RotateCcw, Save, ShieldCheck } from "lucide-react";

import { Button } from "../components/ui/button";
import { Suspense, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { PanelHeader } from "../app/PanelHeader";
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
}: {
  category: string;
  title?: string;
  emptyHint?: string;
  footer?: ReactNode;
}) {
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery(settingsSchemaQuery());
  const groups = useMemo(
    () => data.groups.filter((g) => (g.category || "Plugins") === category),
    [data.groups, category],
  );
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
    mutationFn: () => api.saveSettings(dirty),
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
            : runtimeField
              ? `runtime: ${acpAgent ? `${acpAgent} (ACP)` : "native"}`
              : "applies on save"
        }
        actions={
          <>
            {hasModel ? (
              <button className="secondary-button" type="button" onClick={() => testConn.mutate()} disabled={testConn.isPending || save.isPending}>
                {testConn.isPending ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                Test connection
              </button>
            ) : null}
            {/* Pilot shadcn/Radix component (ADR 0037 S1) — themed by the brand tokens. */}
            <Button variant="secondary" size="sm" type="button" onClick={discard} disabled={save.isPending || !dirtyKeys.length}>
              <RotateCcw size={15} /> Discard
            </Button>
            <button className="primary-button" type="button" onClick={() => save.mutate()} disabled={save.isPending || !dirtyKeys.length}>
              <Save size={16} /> Save &amp; apply
            </button>
          </>
        }
      />
      <div className="stage-body">
        {acpAgent ? (
          <div className="settings-banner runtime-banner">
            <Bot size={14} />
            <span>
              Running on <strong>{acpAgent}</strong> (ACP) — it drives each turn with its own tools.
              The model settings below power protoAgent's own calls (compaction, goal checks); with no
              gateway key configured, those run on {acpAgent} too.
            </span>
          </div>
        ) : null}
        {pendingRestart.length ? (
          <div className="settings-banner" role="alert">
            <AlertTriangle size={14} />
            <span>Needs a restart to take effect: {pendingRestart.join(", ")}</span>
          </div>
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
              />
            ))}
            {hasDiscord && group.section === "Discord" ? (
              <div className="settings-group-actions">
                <button className="secondary-button" type="button" onClick={() => testDiscord.mutate()} disabled={testDiscord.isPending || save.isPending}>
                  {testDiscord.isPending ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                  Test connection
                </button>
                <a className="settings-help-link" href={DISCORD_GUIDE_URL} target="_blank" rel="noreferrer">
                  How to create a bot <ExternalLink size={13} />
                </a>
              </div>
            ) : null}
            {hasGoogle && group.section === "Google" ? (
              <div className="settings-group-actions">
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => googleConnect.mutate()}
                  disabled={googleConnect.isPending || save.isPending || dirtyGoogleClient}
                  title={dirtyGoogleClient ? "Save the client ID + secret first" : undefined}
                >
                  {googleConnect.isPending ? <Loader2 className="spin" size={15} /> : <Link2 size={15} />}
                  {googleStatus.data?.connected ? "Reconnect Google" : "Connect Google"}
                </button>
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
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => { setTestingSection(group.section); testGroup.mutate({ endpoint: group.test!.endpoint, fields: groupFields(group) }); }}
                  disabled={(testGroup.isPending && testingSection === group.section) || save.isPending}
                >
                  {testGroup.isPending && testingSection === group.section ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                  Test connection
                </button>
              </div>
            ) : null}
          </section>
        ))}
        {footer}
      </div>
    </>
  );
}

function SettingRow({ field, value, dirty, onChange }: { field: SettingsField; value: unknown; dirty: boolean; onChange: (value: unknown) => void }) {
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
        {field.options.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
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
        onChange={(e) => onChange(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))}
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
