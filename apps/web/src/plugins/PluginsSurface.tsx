import "../settings/plugins.css";

import { Badge, Button } from "@protolabsai/ui/primitives";
import { Alert, Table, TBody, Td, Th, THead, Tr } from "@protolabsai/ui/data";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

import { useState, type JSX } from "react";
import { ChevronDown, ChevronUp, Download, DownloadCloud, ExternalLink, Github, RefreshCw, Search, Settings2, Store, Trash2 } from "lucide-react";

import { Input } from "@protolabsai/ui/forms";
import { PanelHeader, Tabs } from "@protolabsai/ui/navigation";
import { installedPluginsQuery, pluginUpdatesQuery, queryKeys, runtimeStatusQuery, settingsSchemaQuery } from "../lib/queries";
import { StagePanel } from "../app/ErrorBoundary";
import { errMsg } from "../lib/format";
import { StatusPill } from "../app/StatusPill";
import { InstallPluginDialog } from "./InstallPluginDialog";
import { PluginSettingsDialog } from "./PluginSettingsDialog";
import { PluginFreshness } from "./PluginFreshness";
import { usePluginManage, usePluginRefresh } from "./usePluginManage";
import { catalogCategories, filterCatalog } from "./catalog";
import {
  filterInstalled,
  needsAttention,
  sortInstalled,
  statusCounts,
  type InstalledRow,
  type InstalledSort,
  type InstalledSortKey,
  type InstalledStatus,
} from "./installed";
import { api } from "../lib/api";
import type { CatalogPlugin, PluginUpdate, RuntimeStatus } from "../lib/types";

type Plugin = NonNullable<RuntimeStatus["plugins"]>[number];

const DIRECTORY_URL = "https://agent.protolabs.studio/plugins";
const TOPIC_URL = "https://github.com/topics/protoagent-plugin";

// The error text moved out of this label and onto the Status pill's tooltip when the
// list became a table — the label is purely the tools/skills/views summary now.
function contributionsLabel(p: Plugin): string {
  return (
    [
      p.loaded && p.tools.length ? `${p.tools.length} tool${p.tools.length === 1 ? "" : "s"}` : null,
      p.loaded && p.skills ? `${p.skills} skill${p.skills === 1 ? "" : "s"}` : null,
      p.views?.length ? `${p.views.length} view${p.views.length === 1 ? "" : "s"}` : null,
    ].filter(Boolean).join(" · ") || "—"
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
  depsMissing,
  onInstallDeps,
  installingDeps,
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
  depsMissing?: string[];
  onInstallDeps?: (p: Plugin) => void;
  installingDeps?: boolean;
}) {
  const on = p.enabled;
  const [configOpen, setConfigOpen] = useState(false);
  return (
    <Tr>
      {/* Name · version · (only-when-actionable) freshness badge. */}
      <Td className="plugin-cell-name">
        <div className="plugin-row-head">
          <strong>{p.name}</strong>
          {p.version ? <span className="plugin-ver">v{p.version}</span> : null}
          <PluginFreshness update={update} />
        </div>
      </Td>
      {/* The loaded/disabled state was the SECTION a row sat under before the table
          rework; now it's a per-row pill (and a sortable/filterable column). */}
      <Td className="plugin-cell-status">
        <div className="plugin-status-chips">
          <StatusPill label={p.loaded ? "loaded" : "disabled"} tone={p.loaded ? "success" : "muted"} />
          {p.error ? (
            <span title={p.error}>
              <StatusPill label="error" tone="error" />
            </span>
          ) : null}
          {/* Required-config gate (#1719) — a loaded-but-unconfigured plugin's tools
              return a needs-setup notice; flag it so the operator can finish setup. */}
          {p.incomplete ? (
            <Badge status="warning">
              <span
                title={`Missing required config: ${(p.needs_config ?? []).map((n) => n.label).join(", ") || "setup needed"} — click "Set up"`}
              >
                needs setup
              </span>
            </Badge>
          ) : null}
        </div>
      </Td>
      <Td className="plugin-cell-contrib">{contributionsLabel(p)}</Td>
      <Td className="plugin-cell-actions">
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
              title={update.latest_ref ? `Update ${p.name} to ${update.latest_ref}` : `Update ${p.name} to the latest commit`}
              aria-label={`Update ${p.name}`}
            >
              <RefreshCw size={15} />
            </Button>
          ) : null}
          {/* Missing declared pip deps (previously an "install manually" advisory with
              no in-app action): a labeled install button — pip runs server-side via
              POST /api/plugins/install-deps. */}
          {depsMissing?.length && onInstallDeps ? (
            <Button
              type="button"
              variant="default"
              size="sm"
              loading={installingDeps}
              onClick={() => onInstallDeps(p)}
              title={`Install ${depsMissing.join(", ")}`}
            >
              Install deps
            </Button>
          ) : null}
          {/* Configure opens a per-plugin settings dialog (ADR 0059) rather than expanding
              the row, so the row stays one line and the form gets room. An INCOMPLETE
              plugin (#1719) gets a prominent labeled "Set up" instead of the gear icon —
              it's the primary thing to do on that row. */}
          {p.incomplete ? (
            <Button
              type="button"
              variant="default"
              size="sm"
              onClick={() => setConfigOpen(true)}
              title={`Finish setting up ${p.name}`}
            >
              Set up
            </Button>
          ) : configurable ? (
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
        {configurable || p.incomplete ? (
          <PluginSettingsDialog
            pluginId={p.id}
            pluginName={p.name}
            needsConfig={p.incomplete ? p.needs_config : undefined}
            open={configOpen}
            onClose={() => setConfigOpen(false)}
          />
        ) : null}
      </Td>
    </Tr>
  );
}

// A sortable column header: click toggles direction on the active key, or switches
// key (at its natural order). aria-sort keeps it screen-reader-legible.
function SortableTh({
  label,
  col,
  sort,
  onSort,
}: {
  label: string;
  col: InstalledSortKey;
  sort: InstalledSort;
  onSort: (s: InstalledSort) => void;
}) {
  const active = sort.key === col;
  return (
    <Th
      className="plugin-th-sortable"
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
    >
      <button
        type="button"
        className="plugin-th-btn"
        onClick={() => onSort(active ? { key: col, dir: sort.dir === "asc" ? "desc" : "asc" } : { key: col, dir: "asc" })}
        title={`Sort by ${label.toLowerCase()}`}
      >
        {label}
        {active ? (sort.dir === "asc" ? <ChevronUp size={12} /> : <ChevronDown size={12} />) : null}
      </button>
    </Th>
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
  // Update + uninstall mutations (toast + query-refresh) shared with the rail context
  // menu (#1521 / #1522), so both entry points behave identically. `refreshAll` is the
  // shared installed-set invalidation (runtime + inventory + freshness + the settings
  // schema, which carries each enabled plugin's declared config fields — #1423/#1643).
  const { update, remove } = usePluginManage();
  const refreshAll = usePluginRefresh();

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

  const onUpdate = (p: Plugin) => update.mutate({ id: p.id, name: p.name });
  const updatingId = update.isPending ? update.variables?.id : undefined;
  const updateById = new Map((updates.data?.plugins ?? []).map((u) => [u.id, u]));

  // Uninstall (DELETE /api/plugins/{id}) — removes the code + plugins.lock / enabled refs.
  // Refused server-side for in-tree built-ins, so it's only offered for plugins in the
  // lock-backed inventory. The confirm gates the shared `remove` mutation.
  const onRemove = (p: Plugin) => setUninstallPending(p);
  const removingId = remove.isPending ? remove.variables?.id : undefined;

  // One-click pip install for declared requires_pip (POST /api/plugins/install-deps).
  // refreshAll refetches the installed inventory, so the missing-deps state clears.
  const installDeps = useMutation({
    mutationFn: (p: Plugin) => api.installPluginDeps(p.id),
    onSuccess: (res, p) => {
      toast({
        tone: "success",
        title: "Dependencies installed",
        message: `${p.name}: ${res.installed.join(", ") || "nothing to install"}.`,
      });
      refreshAll();
    },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't install deps", message: `${p.name}: ${errMsg(err)}` }),
  });

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

  // Table controls: free-text search, status chip, sortable columns. Pure logic lives
  // in installed.ts (the catalog.ts pattern) so it's unit-tested; default order keeps
  // the old sections' semantics — loaded first, then name.
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<InstalledStatus>("All");
  const [sort, setSort] = useState<InstalledSort>({ key: "status", dir: "asc" });

  // Built-ins (core runtime infrastructure like the delegate registry) aren't optional
  // add-ons — they always load, can't be toggled, and are configured in Workspace
  // settings — so they don't belong in the install/enable list.
  const plugins = (runtime.plugins ?? []).filter((p) => !p.builtin);
  const depsById = new Map((installed.data?.plugins ?? []).map((e) => [e.id, e.deps_missing ?? []]));
  const rows: InstalledRow[] = plugins.map((p) => ({
    p,
    behind: Boolean(updateById.get(p.id)?.behind),
    depsMissing: depsById.get(p.id) ?? [],
  }));
  const counts = statusCounts(rows);
  const shown = sortInstalled(filterInstalled(rows, q, status), sort);

  const renderRow = ({ p }: InstalledRow) => (
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
      depsMissing={depsById.get(p.id) ?? []}
      onInstallDeps={(pl) => installDeps.mutate(pl)}
      installingDeps={installDeps.isPending && installDeps.variables?.id === p.id}
    />
  );

  return (
    <>
      <PanelHeader title="Installed" kicker={`${counts.All} installed · ${counts.Loaded} loaded`} />
      <div className="stage-body">
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
            <div className="plugin-installed-controls">
              <Input
                className="plugin-search"
                icon={<Search size={14} />}
                type="search"
                placeholder="Search plugins, tools…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                aria-label="Search installed plugins"
              />
              <Tabs
                variant="segmented"
                responsive
                ariaLabel="filter installed plugins by status"
                items={(["All", "Loaded", "Disabled", "Attention"] as const).map((s) => ({
                  id: s,
                  label: counts[s] ? `${s} · ${counts[s]}` : s,
                }))}
                active={status}
                onSelect={(id) => setStatus(id as InstalledStatus)}
              />
              <Button type="button" variant="ghost" onClick={() => setInstallOpen(true)} title="Install a plugin from a git URL">
                <Download size={14} /> Install from URL
              </Button>
            </div>
            <div className="plugin-table-wrap">
              <Table className="plugin-table">
                <THead>
                  <Tr>
                    <SortableTh label="Plugin" col="name" sort={sort} onSort={setSort} />
                    <SortableTh label="Status" col="status" sort={sort} onSort={setSort} />
                    <SortableTh label="Contributes" col="contributions" sort={sort} onSort={setSort} />
                    <Th className="plugin-th-actions" aria-label="actions" />
                  </Tr>
                </THead>
                <TBody>
                  {shown.map(renderRow)}
                  {!shown.length ? (
                    <Tr>
                      <Td colSpan={4} className="muted">
                        No plugins match{q ? ` "${q}"` : ""}{status !== "All" ? ` in ${status}` : ""}.
                      </Td>
                    </Tr>
                  ) : null}
                </TBody>
              </Table>
            </div>
          </>
        ) : (
          <div className="table-list">
            <div className="table-row">
              <span>no plugins installed — browse the Discover tab, or Install from URL</span>
              <Button type="button" variant="ghost" onClick={() => setInstallOpen(true)} title="Install a plugin from a git URL">
                <Download size={14} /> Install from URL
              </Button>
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
        onConfirm={() => { if (uninstallPending) remove.mutate({ id: uninstallPending.id, name: uninstallPending.name }); setUninstallPending(null); }}
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
  const refreshAll = usePluginRefresh();

  const install = useMutation({
    mutationFn: (p: CatalogPlugin) => api.installPlugin(p.repo),
    onSuccess: (res, p) => {
      qc.invalidateQueries({ queryKey: ["plugin-catalog"] });
      // Full installed-set refresh — this path used to invalidate only the catalog +
      // runtime, so the (5-min-stale) settings schema kept no group for the new plugin
      // and its Configure dialog opened EMPTY until a page refresh (#1643). It also
      // hid the row's Configure/Uninstall buttons (inventory + schema drive both).
      refreshAll();
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
          <Input
            className="plugin-search"
            icon={<Search size={14} />}
            type="search"
            placeholder="Search plugins…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            aria-label="Search plugins"
          />
          <Tabs
            variant="segmented"
            responsive
            ariaLabel="filter plugins by category"
            items={categories.map((c) => ({ id: c, label: c }))}
            active={cat}
            onSelect={setCat}
          />
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
