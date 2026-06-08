import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Package, Plus, ShieldAlert, Trash2 } from "lucide-react";
import { useState } from "react";

import { api } from "../lib/api";
import { installedPluginsQuery, queryKeys } from "../lib/queries";
import type { InstalledPlugin } from "../lib/types";

// Plugins panel (ADR 0027) — install plugins from a git URL, under Settings →
// Integrations. Mirrors the delegates panel. Read non-suspense so a 404 shows a
// hint rather than blanking Settings. Install fetches code only (install ≠
// enable): enabling stays a config + restart decision, surfaced here.
const REGISTRY_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/plugin-registry";

export function PluginsSection() {
  const qc = useQueryClient();
  const list = useQuery(installedPluginsQuery());
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [status, setStatus] = useState("");

  const invalidate = () => qc.invalidateQueries({ queryKey: queryKeys.installedPlugins });

  const install = useMutation({
    mutationFn: () => api.installPlugin(url.trim(), ref.trim() || undefined),
    onSuccess: (res) => {
      const s = res.installed;
      const deps = s.requires_pip.length ? ` — declares deps (install manually): ${s.requires_pip.join(", ")}` : "";
      setStatus(`✓ installed ${s.id} v${s.version} @ ${s.resolved_sha.slice(0, 10)} — NOT enabled yet${deps}`);
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

  const plugins = list.data?.plugins ?? [];

  return (
    <section className="settings-section">
      <header className="settings-section-head">
        <h3><Package size={16} /> Install from a git URL</h3>
        <p className="settings-section-sub">
          Install a plugin from a git URL. Fetching code never runs it — review, then{" "}
          <strong>enable</strong> it from the list above (tools apply live; a console view or
          background surface needs a restart). Untrusted code?
          Use an <a href="https://protolabsai.github.io/protoAgent/guides/mcp" target="_blank" rel="noreferrer">MCP server</a> instead.{" "}
          <a href={REGISTRY_GUIDE_URL} target="_blank" rel="noreferrer">Guide</a>.
        </p>
      </header>

      {/* Install form */}
      <div className="plugin-install-form">
        <input
          type="text"
          placeholder="https://github.com/owner/protoagent-plugin-x"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          aria-label="plugin git URL"
        />
        <input
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

      {/* Installed list */}
      {list.isError ? (
        <p className="settings-section-sub">Plugin install API unavailable.</p>
      ) : plugins.length === 0 ? (
        <p className="settings-section-sub">No git-installed plugins yet.</p>
      ) : (
        <ul className="plugin-list">
          {plugins.map((p) => <PluginRow key={p.id} p={p} onRemove={() => remove.mutate(p.id)} removing={remove.isPending} />)}
        </ul>
      )}
    </section>
  );
}

function PluginRow({ p, onRemove, removing }: { p: InstalledPlugin; onRemove: () => void; removing: boolean }) {
  const m = p.manifest;
  const caps = m?.capabilities && Object.keys(m.capabilities).length ? m.capabilities : null;
  return (
    <li className="plugin-row">
      <div className="plugin-row-main">
        <div className="plugin-row-title">
          <strong>{m?.name || p.id}</strong>
          {m?.version ? <span className="plugin-ver">v{m.version}</span> : null}
          <span className={`plugin-state ${p.enabled ? "on" : "off"}`}>{p.enabled ? "enabled" : "not enabled"}</span>
          {!p.present ? <span className="plugin-state warn"><AlertTriangle size={12} /> missing — run sync</span> : null}
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
            To enable: use the <strong>Enable</strong> button in the list above (or add{" "}
            <code>{p.id}</code> to <code>plugins.enabled</code> in config).
          </p>
        ) : null}
      </div>
      <button className="btn-icon danger" title="Uninstall" disabled={removing} onClick={onRemove} aria-label={`uninstall ${p.id}`}>
        <Trash2 size={15} />
      </button>
    </li>
  );
}
