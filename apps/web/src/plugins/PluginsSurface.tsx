import "../settings/plugins.css";

import { Button } from "@protolabsai/ui/primitives";
import { Alert } from "@protolabsai/ui/data";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

import { useState, type JSX } from "react";
import { Download, DownloadCloud, ExternalLink, Github, RefreshCw, Search, Settings2, Store, Trash2 } from "lucide-react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { installedPluginsQuery, pluginUpdatesQuery, queryKeys, runtimeStatusQuery, settingsSchemaQuery } from "../lib/queries";
import { StagePanel } from "../app/ErrorBoundary";
import { errMsg } from "../lib/format";
import { StatusPill } from "../app/StatusPill";
import { InstallPluginDialog } from "./InstallPluginDialog";
import { PluginSettingsDialog } from "./PluginSettingsDialog";
import { PluginFreshness } from "./PluginFreshness";
import { catalogCategories, filterCatalog } from "./catalog";
import { api } from "../lib/api";
import type { CatalogPlugin, PluginUpdate, RuntimeStatus } from "../lib/types";

type Plugin = NonNullable<RuntimeStatus["plugins"]>[number];

const DIRECTORY_URL = "https://agent.protolabs.studio/plugins";
const TOPIC_URL = "https://github.com/topics/protoagent-plugin";

function contributionsLabel(p: Plugin): string {
  return (
    [
      p.loaded && p.tools.length ? `${p.tools.length} tool${p.tools.length === 1 ? "" : "s"}` : null,
      p.loaded && p.skills ? `${p.skills} skill${p.skills === 1 ? "" : "s"}` : null,
      p.views?.length ? `${p.views.length} view${p.views.length === 1 ? "" : "s"}` : null,
      p.error || null,
    ].filter(Boolean).join(" · ") || "no contributions"
  );
}

function PluginRow({
  p,
  update,
  busy,
  onToggle,
  onUpdate,
  updating,
  configurable,
  removable,
  onRemove,
  removing,
}: {
  p: Plugin;
  update?: PluginUpdate;
  busy: boolean;
  onToggle: (p: Plugin) => void;
  onUpdate: (p: Plugin) => void;
  updating: boolean;
  configurable: boolean;
  removable: boolean;
  onRemove: (p: Plugin) => void;
  removing: boolean;
}) {
  const on = p.enabled;
  const [configOpen, setConfigOpen] = useState(false);
  return (
    <div className="plugin-row-wrap">
      <div className="subagent-row">
        <div>
          {/* Name · version · (only-when-actionable) freshness/error badge. The loaded
              vs disabled state is already the section this row sits under, so it carries
              no per-row status pill — only a genuine error gets a badge. */}
          <div className="plugin-row-head">
            <strong>{p.name}</strong>
            {p.version ? <span className="plugin-ver">v{p.version}</span> : null}
            <PluginFreshness update={update} />
            {p.error ? <StatusPill label="error" tone="error" /> : null}
          </div>
          <span>{contributionsLabel(p)}</span>
        </div>
        {/* Compact action cluster: secondary actions (update / configure / uninstall) are
            icon-only with tooltips; only the primary Enable/Disable toggle keeps its label. */}
        <div className="plugin-row-actions">
          {update?.behind ? (
            <Button
              type="button"
              icon
              variant="ghost"
              loading={updating}
              onClick={() => onUpdate(p)}
              title={`Update ${p.name} to the latest commit`}
              aria-label={`Update ${p.name}`}
            >
              <RefreshCw size={15} />
            </Button>
          ) : null}
          {/* Configure opens a per-plugin settings dialog (ADR 0059) rather than expanding
              the row, so the row stays one line and the form gets room. */}
          {configurable ? (
            <Button
              type="button"
              icon
              variant="ghost"
              onClick={() => setConfigOpen(true)}
              title={`Configure ${p.name}`}
              aria-label={`Configure ${p.name}`}
            >
              <Settings2 size={15} />
            </Button>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            loading={busy}
            onClick={() => onToggle(p)}
            title={on ? `Disable ${p.name}` : `Enable ${p.name}`}
          >
            {on ? "Disable" : "Enable"}
          </Button>
          {/* Uninstall — only plugins in the writable plugins dir (git-installed / local
              copies) are removable; in-tree built-ins are refused server-side, so they
              only get Disable. */}
          {removable ? (
            <Button
              type="button"
              icon
              variant="ghost"
              className="plugin-row-danger"
              loading={removing}
              onClick={() => onRemove(p)}
              title={`Uninstall ${p.name}`}
              aria-label={`uninstall ${p.id}`}
            >
              <Trash2 size={15} />
            </Button>
          ) : null}
        </div>
      </div>
      {configurable ? (
        <PluginSettingsDialog
          pluginId={p.id}
          pluginName={p.name}
          open={configOpen}
          onClose={() => setConfigOpen(false)}
        />
      ) : null}
    </div>
  );
}

type PluginsTab = "local" | "market";

// Installed — the single plugin manager: every installed plugin with enable/disable,
// update, configure, and uninstall (git-installed only); a Sync action for locked-but-
// missing ones; and an Install-from-URL dialog. (ADR 0027 + ADR 0059.)
function LocalTab() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  // Update status (ADR 0027) — joined per plugin id; degrades gracefully (non-suspense,
  // retry:false) so a missing updates API never blanks the list.
  const updates = useQuery(pluginUpdatesQuery());
  // Lock-backed inventory: which plugins live in the writable plugins dir (uninstallable —
  // in-tree built-ins are not) + which are locked-but-missing on disk.
  const installed = useQuery(installedPluginsQuery());
  const qc = useQueryClient();
  const toast = useToast();
  const [installOpen, setInstallOpen] = useState(false);
  const [uninstallPending, setUninstallPending] = useState<Plugin | null>(null);
  const [restartPending, setRestartPending] = useState(false);

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
    qc.invalidateQueries({ queryKey: queryKeys.installedPlugins });
    qc.invalidateQueries({ queryKey: queryKeys.pluginUpdates });
    // The active plugin set changed, so the settings schema (which carries each enabled
    // plugin's declared config fields, ADR 0019) is stale — refetch it (#1423).
    qc.invalidateQueries({ queryKey: queryKeys.settings });
  };

  const toggle = useMutation({
    mutationFn: (p: Plugin) => api.setPluginEnabled(p.id, !p.enabled),
    onSuccess: (res, p) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      // Enable/disable changes which plugins contribute Settings fields (ADR 0019), so
      // refetch the schema — else a just-enabled plugin's config section won't appear
      // until a restart clears the 5-min-stale cache (#1423).
      qc.invalidateQueries({ queryKey: queryKeys.settings });
      // Enable hot-mounts the plugin's router (#822). Only DISABLE leaves a stale
      // route/surface behind (FastAPI can't unmount) → restart_recommended on OFF.
      toast(
        res.restart_recommended
          ? { tone: "info", title: "Plugin disabled", message: `${p.name} — restart to fully remove its console view or background surface.` }
          : { tone: "success", title: `Plugin ${res.enabled ? "enabled" : "disabled"}`, message: `${p.name} is ${res.enabled ? "live" : "off"}.` },
      );
    },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't toggle plugin", message: `${p.name}: ${errMsg(err)}` }),
  });
  const onToggle = (p: Plugin) => toggle.mutate(p);
  const pendingId = toggle.isPending ? toggle.variables?.id : undefined;

  const update = useMutation({
    mutationFn: (p: Plugin) => api.updatePlugin(p.id),
    onSuccess: (res, p) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      qc.invalidateQueries({ queryKey: queryKeys.pluginUpdates });
      // A new version may declare new/changed config fields — refetch the schema (#1423).
      qc.invalidateQueries({ queryKey: queryKeys.settings });
      toast(
        res.restart_recommended
          ? { tone: "info", title: "Plugin updated", message: `${p.name}${res.version ? ` to v${res.version}` : ""} — restart to fully load its console view or background surface.` }
          : { tone: "success", title: "Plugin updated", message: `${p.name}${res.version ? ` to v${res.version}` : ""}${res.reloaded ? " (hot-reloaded)" : ""}.` },
      );
    },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't update plugin", message: `${p.name}: ${errMsg(err)}` }),
  });
  const onUpdate = (p: Plugin) => update.mutate(p);
  const updatingId = update.isPending ? update.variables?.id : undefined;
  const updateById = new Map((updates.data?.plugins ?? []).map((u) => [u.id, u]));

  // Uninstall (DELETE /api/plugins/{id}) — removes the code + plugins.lock / enabled refs.
  // Refused server-side for in-tree built-ins, so it's only offered for plugins in the
  // lock-backed inventory.
  const remove = useMutation({
    mutationFn: (p: Plugin) => api.uninstallPlugin(p.id),
    onSuccess: (_res, p) => { refreshAll(); toast({ tone: "success", title: "Plugin uninstalled", message: `${p.name} removed.` }); },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't uninstall plugin", message: `${p.name}: ${errMsg(err)}` }),
  });
  const onRemove = (p: Plugin) => setUninstallPending(p);
  const removingId = remove.isPending ? remove.variables?.id : undefined;

  // Re-clone locked-but-missing plugins (fresh clone / restored data dir).
  const sync = useMutation({
    mutationFn: () => api.syncPlugins(),
    onSuccess: (res) => {
      const fetched = res.plugins.filter((r) => r.status === "installed").map((r) => r.id);
      const failed = res.plugins.filter((r) => r.status === "failed");
      toast(
        failed.length
          ? { tone: "error", title: "Sync had problems", message: `${failed.map((f) => `${f.id} (${f.error ?? "failed"})`).join(", ")}${fetched.length ? ` — fetched ${fetched.join(", ")}` : ""}` }
          : { tone: "success", title: "Plugins synced", message: fetched.length ? `Fetched ${fetched.join(", ")}${res.reloaded ? " — enabled plugins are live" : ""}.` : "All locked plugins present." },
      );
      refreshAll();
    },
    onError: (err: unknown) => toast({ tone: "error", title: "Couldn't sync", message: errMsg(err) }),
  });

  const restart = useMutation({
    mutationFn: () => api.restart(),
    onSuccess: () => toast({ tone: "info", title: "Restarting server", message: "The console will reconnect when it's back." }),
    onError: (err: unknown) => toast({ tone: "error", title: "Couldn't restart", message: errMsg(err) }),
  });

  // Which plugins have settings to fold in (ADR 0059) — the schema's plugin-tagged groups.
  const schema = useQuery(settingsSchemaQuery());
  const configurableIds = new Set(
    (schema.data?.groups ?? []).filter((g) => g.plugin_id).map((g) => g.plugin_id as string),
  );
  const removableIds = new Set((installed.data?.plugins ?? []).map((e) => e.id));
  const missing = (installed.data?.plugins ?? []).filter((e) => !e.present);

  // Built-ins (core runtime infrastructure like the delegate registry) aren't optional
  // add-ons — they always load, can't be toggled, and are configured in Workspace
  // settings — so they don't belong in the install/enable list.
  const plugins = (runtime.plugins ?? []).filter((p) => !p.builtin);
  const byName = (a: Plugin, b: Plugin) => a.name.localeCompare(b.name);
  const loaded = plugins.filter((p) => p.loaded).sort(byName);
  const disabled = plugins.filter((p) => !p.loaded).sort(byName);

  const renderRow = (p: Plugin) => (
    <PluginRow
      key={p.id}
      p={p}
      update={updateById.get(p.id)}
      busy={pendingId === p.id}
      onToggle={onToggle}
      onUpdate={onUpdate}
      updating={updatingId === p.id}
      configurable={configurableIds.has(p.id)}
      removable={removableIds.has(p.id)}
      onRemove={onRemove}
      removing={removingId === p.id}
    />
  );

  return (
    <>
      <PanelHeader title="Installed" kicker={`${plugins.length} installed · ${loaded.length} loaded`} />
      <div className="stage-body">
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
          <Button type="button" variant="ghost" onClick={() => setInstallOpen(true)} title="Install a plugin from a git URL">
            <Download size={14} /> Install from URL
          </Button>
        </div>

        {missing.length ? (
          <Alert
            status="warning"
            action={
              <Button type="button" variant="default" size="sm" loading={sync.isPending} onClick={() => sync.mutate()} title="Re-clone every locked plugin at its pinned commit">
                {sync.isPending ? null : <DownloadCloud size={13} />} Sync plugins
              </Button>
            }
          >
            {missing.length === 1 ? <><code>{missing[0].id}</code> is</> : <>{missing.length} plugins are</>}{" "}
            in <code>plugins.lock</code> but missing on disk.
          </Alert>
        ) : null}

        {plugins.length ? (
          <>
            {loaded.length ? (
              <>
                <p className="panel-kicker">Loaded <span className="muted">· {loaded.length}</span></p>
                <div className="subagent-list">{loaded.map(renderRow)}</div>
              </>
            ) : null}
            {disabled.length ? (
              <>
                <p className="panel-kicker">Disabled <span className="muted">· {disabled.length}</span></p>
                <div className="subagent-list">{disabled.map(renderRow)}</div>
              </>
            ) : null}
          </>
        ) : (
          <div className="table-list">
            <div className="table-row">
              <span>no plugins installed — browse the Discover tab, or Install from URL</span>
              <StatusPill label="none" tone="muted" />
            </div>
          </div>
        )}

        {/* Server restart — a plugin's console view / background surface (and env / launch
            flags) only fully (un)load on restart. The console reconnects on its own. */}
        <div className="plugin-restart-row">
          <span className="settings-section-sub">
            A plugin's console view or background surface — and env / launch-flag changes — need a
            server restart to take effect.
          </span>
          <Button
            type="button"
            variant="default"
            size="sm"
            loading={restart.isPending}
            onClick={() => setRestartPending(true)}
            title="Gracefully restart the server process"
          >
            {restart.isPending ? null : <RefreshCw size={13} />} Restart server
          </Button>
        </div>
      </div>
      <InstallPluginDialog open={installOpen} onClose={() => setInstallOpen(false)} />
      <ConfirmDialog
        open={uninstallPending !== null}
        title="Uninstall plugin?"
        confirmLabel="Uninstall"
        destructive
        onConfirm={() => { if (uninstallPending) remove.mutate(uninstallPending); setUninstallPending(null); }}
        onClose={() => setUninstallPending(null)}
      >
        {uninstallPending
          ? `"${uninstallPending.name}" — this deletes its code from disk and removes it from plugins.lock. To keep it installed, Disable it instead.`
          : undefined}
      </ConfirmDialog>
      <ConfirmDialog
        open={restartPending}
        title="Restart the server?"
        confirmLabel="Restart"
        onConfirm={() => { restart.mutate(); setRestartPending(false); }}
        onClose={() => setRestartPending(false)}
      >
        In-flight work finishes, then the console reconnects automatically.
      </ConfirmDialog>
    </>
  );
}

// Discover — the in-app official-plugin directory (ADR 0059): browse the curated
// catalog + one-click install (runtime install, works on every surface incl. the
// frozen desktop app via ADR 0058).
function DiscoverTab() {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["plugin-catalog"], queryFn: () => api.pluginCatalog(), retry: false });
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("All");
  const toast = useToast();

  const install = useMutation({
    mutationFn: (p: CatalogPlugin) => api.installPlugin(p.repo),
    onSuccess: (res, p) => {
      qc.invalidateQueries({ queryKey: ["plugin-catalog"] });
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      toast({ tone: "success", title: "Plugin installed", message: `${p.name}${res.reloaded ? " — enabled and live" : ""}.` });
    },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't install plugin", message: `${p.name}: ${errMsg(err)}` }),
  });
  const installingRepo = install.isPending ? install.variables?.repo : undefined;

  const plugins = catalog.data?.plugins ?? [];
  const categories = catalogCategories(plugins);
  const shown = filterCatalog(plugins, q, cat);

  return (
    <>
      <PanelHeader title="Discover" kicker={`${plugins.length} official plugins`} />
      <div className="stage-body">
        <div className="plugin-discover-controls">
          <div className="plugin-search">
            <Search size={14} />
            <input placeholder="Search plugins…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search plugins" />
          </div>
          <div className="plugin-cats">
            {categories.map((c) => (
              <button key={c} type="button" className={c === cat ? "plugin-cat on" : "plugin-cat"} onClick={() => setCat(c)}>{c}</button>
            ))}
          </div>
        </div>
        {catalog.isLoading ? <p className="muted">Loading directory…</p> : null}
        {catalog.isError ? <p className="plugin-hint">Couldn't load the catalog: {errMsg(catalog.error)}</p> : null}
        <div className="plugin-card-grid">
          {shown.map((p) => (
            <div className="plugin-card" key={p.id}>
              <div className="plugin-card-head">
                <strong>{p.name}</strong>
                {p.category ? <span className="plugin-chip">{p.category}</span> : null}
              </div>
              <p className="plugin-card-tagline">{p.tagline}</p>
              <div className="plugin-card-foot">
                <a className="plugin-card-repo" href={p.repo} target="_blank" rel="noopener noreferrer">
                  <Github size={13} /> repo <ExternalLink size={11} />
                </a>
                {p.bundled ? (
                  <StatusPill label="bundled" tone="muted" />
                ) : p.installed ? (
                  <StatusPill label={p.enabled ? "installed · on" : "installed"} tone="success" />
                ) : (
                  <Button type="button" loading={installingRepo === p.repo} disabled={install.isPending} onClick={() => install.mutate(p)}>
                    {installingRepo === p.repo ? null : <Download size={14} />} Install
                  </Button>
                )}
              </div>
            </div>
          ))}
          {!shown.length && !catalog.isLoading ? <p className="muted">No plugins match.</p> : null}
        </div>
        <div className="plugin-market" style={{ marginTop: 14 }}>
          <a className="plugin-market-link" href={DIRECTORY_URL} target="_blank" rel="noopener noreferrer">
            <Store size={16} />
            <span><strong>Full directory</strong><span className="muted">Curated + community plugins online</span></span>
            <ExternalLink size={14} />
          </a>
          <a className="plugin-market-link" href={TOPIC_URL} target="_blank" rel="noopener noreferrer">
            <Github size={16} />
            <span><strong>GitHub topic</strong><span className="muted">Every repo tagged <code>protoagent-plugin</code></span></span>
            <ExternalLink size={14} />
          </a>
        </div>
      </div>
    </>
  );
}

const TABS: Record<PluginsTab, () => JSX.Element> = {
  local: LocalTab,
  market: DiscoverTab,
};

export function PluginsSurface({ tab = "local" }: { tab?: PluginsTab }) {
  const Body = TABS[tab] ?? LocalTab;
  return (
    <StagePanel label="plugins">
      <Body />
    </StagePanel>
  );
}
