import "./plugins.css";

import { Button } from "@protolabsai/ui/primitives";
import { Input } from "@protolabsai/ui/forms";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, DownloadCloud, Loader2, Package, Plus, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import { useState } from "react";

import { api } from "../lib/api";
import { installedPluginsQuery, pluginUpdatesQuery, queryKeys } from "../lib/queries";
import { PluginFreshness } from "../plugins/PluginFreshness";
import type { InstalledPlugin, PluginUpdate } from "../lib/types";

// Plugins panel (ADR 0027) — install plugins from a git URL, under Settings →
// Integrations. Mirrors the delegates panel. Read non-suspense so a 404 shows a
// hint rather than blanking Settings. Installing AUTO-ENABLES + runs the plugin
// (trust-by-default) — the console flashes a one-time "runs code" confirm for
// unofficial sources first.
const REGISTRY_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/plugin-registry";

export function PluginsSection() {
  const qc = useQueryClient();
  const list = useQuery(installedPluginsQuery());
  const updates = useQuery(pluginUpdatesQuery());
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [status, setStatus] = useState("");

  const invalidate = () => qc.invalidateQueries({ queryKey: queryKeys.installedPlugins });
  // After an update we re-read BOTH the installed list (new resolved_sha) and the
  // freshness probe (so the badge flips to "up to date").
  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: queryKeys.installedPlugins });
    qc.invalidateQueries({ queryKey: queryKeys.pluginUpdates });
  };

  const updateMut = useMutation({
    mutationFn: (id: string) => api.updatePlugin(id),
    onSuccess: (res) => {
      // Mirror the enable flow's restart-hint contract: a view/route plugin can't
      // swap its mounted router live, so updating it recommends a restart.
      setStatus(
        res.restart_recommended
          ? `✓ updated ${res.id}${res.version ? ` to v${res.version}` : ""} — restart to fully load its console view or background surface.`
          : `✓ updated ${res.id}${res.version ? ` to v${res.version}` : ""}${res.reloaded ? " (hot-reloaded)" : ""}.`,
      );
      invalidateAll();
    },
    onError: (e: unknown) => setStatus(`✗ ${e instanceof Error ? e.message : "update failed"}`),
  });
  const updateById = new Map((updates.data?.plugins ?? []).map((u) => [u.id, u]));

  const install = useMutation({
    mutationFn: () => api.installPlugin(url.trim(), ref.trim() || undefined),
    onSuccess: (res) => {
      const s = res.installed;
      const who = res.enabled.length ? res.enabled.join(", ") : (s.id ?? "plugin");
      const deps = s.requires_pip?.length ? ` — declares deps (install manually): ${s.requires_pip.join(", ")}` : "";
      setStatus(
        res.enable_error
          ? `✓ installed ${who} — auto-enable failed (${res.enable_error}); enable it on the Local tab${deps}`
          : res.reloaded
            ? `✓ installed + enabled ${who} — it's live${deps}`
            : `✓ installed ${who}${deps}`,
      );
      setUrl("");
      setRef("");
      invalidate();
    },
    onError: (e: unknown) => setStatus(`✗ ${e instanceof Error ? e.message : "install failed"}`),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.uninstallPlugin(id),
    onSuccess: () => { setStatus("✓ uninstalled"); invalidate(); },
    onError: (e: unknown) => setStatus(`✗ ${e instanceof Error ? e.message : "uninstall failed"}`),
  });

  const sync = useMutation({
    mutationFn: () => api.syncPlugins(),
    onSuccess: (res) => {
      const fetched = res.plugins.filter((r) => r.status === "installed").map((r) => r.id);
      const failed = res.plugins.filter((r) => r.status === "failed");
      setStatus(
        failed.length
          ? `✗ sync: ${failed.map((f) => `${f.id} (${f.error ?? "failed"})`).join(", ")}${fetched.length ? ` — fetched ${fetched.join(", ")}` : ""}`
          : fetched.length
            ? `✓ fetched ${fetched.join(", ")}${res.reloaded ? " — enabled plugins are live" : ""}${res.reload_error ? ` — reload failed (${res.reload_error})` : ""}`
            : "✓ nothing to sync — all locked plugins present",
      );
      invalidateAll();
    },
    onError: (e: unknown) => setStatus(`✗ ${e instanceof Error ? e.message : "sync failed"}`),
  });

  const plugins = list.data?.plugins ?? [];
  const missing = plugins.filter((p) => !p.present);

  return (
    <section className="settings-section">
      <header className="settings-section-head">
        <h3><Package size={16} /> Install from a git URL</h3>
        <p className="settings-section-sub">
          Install a plugin from a git URL. Installing <strong>enables and runs it</strong>{" "}
          immediately — its tools, console views and background surfaces come up live, no
          separate enable step and no restart. Only install code you trust; for untrusted
          code use an <a href="https://protolabsai.github.io/protoAgent/guides/mcp" target="_blank" rel="noreferrer">MCP server</a> instead.{" "}
          <a href={REGISTRY_GUIDE_URL} target="_blank" rel="noreferrer">Guide</a>.
        </p>
      </header>

      {/* Install form */}
      <div className="plugin-install-form">
        <Input
          type="text"
          placeholder="https://github.com/owner/protoagent-plugin-x"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          aria-label="plugin git URL"
        />
        <Input
          type="text"
          placeholder="ref (tag / sha — optional)"
          value={ref}
          onChange={(e) => setRef(e.target.value)}
          aria-label="git ref"
          style={{ maxWidth: 200 }}
        />
        <button
          className="btn"
          disabled={!url.trim() || install.isPending}
          onClick={() => { setStatus(""); install.mutate(); }}
        >
          {install.isPending ? <Loader2 className="spin" size={15} /> : <Plus size={15} />} Install
        </button>
      </div>
      {status ? <p className="plugin-install-status" role="status">{status}</p> : null}

      {/* Locked-but-missing plugins (fresh clone / restored data dir) → one-click sync */}
      {missing.length ? (
        <div className="plugin-sync-banner" role="status">
          <AlertTriangle size={14} />
          <span>
            {missing.length === 1 ? <><code>{missing[0].id}</code> is</> : <>{missing.length} plugins are</>}{" "}
            in <code>plugins.lock</code> but missing on disk.
          </span>
          <Button
            type="button"
            variant="default"
            size="sm"
            disabled={sync.isPending}
            onClick={() => { setStatus(""); sync.mutate(); }}
            title="Re-clone every locked plugin at its pinned commit"
          >
            {sync.isPending ? <Loader2 size={13} className="spin" /> : <DownloadCloud size={13} />} Sync plugins
          </Button>
        </div>
      ) : null}

      {/* Installed list */}
      {list.isError ? (
        <p className="settings-section-sub">Plugin install API unavailable.</p>
      ) : plugins.length === 0 ? (
        <p className="settings-section-sub">No git-installed plugins yet.</p>
      ) : (
        <ul className="plugin-list">
          {plugins.map((p) => (
            <PluginRow
              key={p.id}
              p={p}
              update={updateById.get(p.id)}
              onRemove={() => remove.mutate(p.id)}
              removing={remove.isPending}
              onUpdate={() => { setStatus(""); updateMut.mutate(p.id); }}
              updating={updateMut.isPending && updateMut.variables === p.id}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function PluginRow({
  p,
  update,
  onRemove,
  removing,
  onUpdate,
  updating,
}: {
  p: InstalledPlugin;
  update?: PluginUpdate;
  onRemove: () => void;
  removing: boolean;
  onUpdate: () => void;
  updating: boolean;
}) {
  const m = p.manifest;
  const caps = m?.capabilities && Object.keys(m.capabilities).length ? m.capabilities : null;
  return (
    <li className="plugin-row">
      <div className="plugin-row-main">
        <div className="plugin-row-title">
          <strong>{m?.name || p.id}</strong>
          {m?.version ? <span className="plugin-ver">v{m.version}</span> : null}
          <PluginFreshness update={update} />
          {update?.behind ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={updating}
              onClick={onUpdate}
              title={`Update ${m?.name || p.id} to the latest commit`}
            >
              {updating ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />} Update
            </Button>
          ) : null}
          <span className={`plugin-state ${p.enabled ? "on" : "off"}`}>{p.enabled ? "enabled" : "not enabled"}</span>
          {!p.present ? <span className="plugin-state warn"><AlertTriangle size={12} /> missing — sync above</span> : null}
        </div>
        {m?.description ? <p className="plugin-desc">{m.description}</p> : null}
        <p className="plugin-meta">
          <span title={p.source_url}>{p.source_url}</span> · <code>{p.resolved_sha.slice(0, 10)}</code>
          {p.requested_ref ? ` · ${p.requested_ref}` : ""}
        </p>
        {/* review surface: what this plugin can do (ADR 0027 D5) */}
        {(m?.views?.length || m?.requires_pip?.length || m?.requires_env?.length || m?.secrets?.length || caps) ? (
          <p className="plugin-review">
            {m?.views?.length ? <span>views: {m.views.join(", ")}</span> : null}
            {m?.requires_pip?.length ? <span className="warn"><ShieldAlert size={12} /> deps (install manually): {m.requires_pip.join(", ")}</span> : null}
            {m?.requires_env?.length ? <span>env: {m.requires_env.join(", ")}</span> : null}
            {m?.secrets?.length ? <span>secrets: {m.secrets.join(", ")}</span> : null}
            {caps ? <span>capabilities: {JSON.stringify(caps)}</span> : null}
          </p>
        ) : null}
        {!p.enabled ? (
          <p className="plugin-enable-hint">
            To enable: use the <strong>Enable</strong> button on the <strong>Local</strong> tab
            (or add <code>{p.id}</code> to <code>plugins.enabled</code> in config).
          </p>
        ) : null}
      </div>
      <button className="btn-icon danger" title="Uninstall" disabled={removing} onClick={onRemove} aria-label={`uninstall ${p.id}`}>
        <Trash2 size={15} />
      </button>
    </li>
  );
}
