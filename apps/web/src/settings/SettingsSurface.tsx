import { QueryErrorResetBoundary, useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, Link2, Loader2, RotateCcw, Save, ShieldCheck } from "lucide-react";
import { Suspense, useMemo, useState } from "react";

// Setup walkthroughs live in the template's (protoAgent) docs — forks don't ship
// their own docs site, so the in-app help links point at the canonical docs.
const DISCORD_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/discord#bot-setup";
const GOOGLE_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/google#oauth-client";

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
    <section className="panel stage-panel settings-panel">
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

  // Category sub-nav (ADR 0020): the server tags each group with a category and
  // orders them, so we derive the ordered category list by first appearance.
  const categories = useMemo(() => {
    const seen: string[] = [];
    for (const g of groups) {
      const c = g.category || "Integrations";
      if (!seen.includes(c)) seen.push(c);
    }
    return seen;
  }, [groups]);
  const [activeCategory, setActiveCategory] = useState(categories[0] || "");
  // The active category must stay valid if the schema reshapes under us.
  const category = categories.includes(activeCategory) ? activeCategory : categories[0] || "";
  const visibleGroups = groups.filter((g) => (g.category || "Integrations") === category);

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

  // Verify the model can actually complete — tests the pending edits if any
  // (e.g. a freshly-typed key), else the saved config (blanks fall back server
  // side). Real completion probe, the same auth path as chat.
  const asStr = (v: unknown) => (typeof v === "string" ? v : "");
  const testConn = useMutation({
    mutationFn: () =>
      api.testModel(
        asStr(dirty["model.api_base"]),
        asStr(dirty["model.api_key"]),
        asStr(dirty["model.name"]),
      ),
    onMutate: () => setStatus("testing connection…"),
    onSuccess: (r) =>
      setStatus(r.ok ? "connection OK — the model responded." : `connection failed — ${r.error || "no response"}`),
    onError: (e) => setStatus(`connection test failed: ${e instanceof Error ? e.message : String(e)}`),
  });

  // Verify a Discord bot token (pending edit or saved). Shows the bot name.
  const testDiscord = useMutation({
    mutationFn: () => api.testDiscord(asStr(dirty["discord.bot_token"])),
    onMutate: () => setStatus("testing Discord…"),
    onSuccess: (r) =>
      setStatus(
        r.ok
          ? `Discord OK — connected as ${r.bot_user || "your bot"}.`
          : `Discord connection failed — ${r.error || "check the token"}`,
      ),
    onError: (e) => setStatus(`Discord test failed: ${e instanceof Error ? e.message : String(e)}`),
  });

  // Google surface (ADR 0017): show connection status + a "Connect Google"
  // button that runs the OAuth consent (opens the operator's browser).
  const googleStatus = useQuery({ queryKey: ["google-status"], queryFn: () => api.googleStatus() });
  const googleConnect = useMutation({
    mutationFn: () => api.googleConnect(),
    onMutate: () => setStatus("opening Google consent in your browser…"),
    onSuccess: (r) => {
      setStatus(
        r.ok
          ? `Google connected${r.email ? ` as ${r.email}` : ""}.`
          : `Google connect failed — ${r.error || "try again"}`,
      );
      void googleStatus.refetch();
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
    },
    onError: (e) => setStatus(`Google connect failed: ${e instanceof Error ? e.message : String(e)}`),
  });
  const dirtyGoogleClient = "google.client_id" in dirty || "google.client_secret" in dirty;

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
          <button className="secondary-button" type="button" onClick={() => testConn.mutate()} disabled={testConn.isPending || save.isPending}>
            {testConn.isPending ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
            Test connection
          </button>
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
      {categories.length > 1 ? (
        <div className="stage-subnav settings-subnav">
          {categories.map((c) => (
            <button key={c} className={c === category ? "active" : ""} onClick={() => setActiveCategory(c)}>
              {c}
            </button>
          ))}
        </div>
      ) : null}
      <div className="stage-body">
        {pendingRestart.length ? (
          <div className="settings-banner" role="alert">
            <AlertTriangle size={14} />
            <span>Needs a restart to take effect: {pendingRestart.join(", ")}</span>
          </div>
        ) : null}
        {status ? <p className="settings-status">{status}</p> : null}

        {visibleGroups.map((group) => (
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
            {group.section === "Discord" ? (
              <div className="settings-group-actions">
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => testDiscord.mutate()}
                  disabled={testDiscord.isPending || save.isPending}
                >
                  {testDiscord.isPending ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}
                  Test connection
                </button>
                <a className="settings-help-link" href={DISCORD_GUIDE_URL} target="_blank" rel="noreferrer">
                  How to create a bot <ExternalLink size={13} />
                </a>
              </div>
            ) : null}
            {group.section === "Google" ? (
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
