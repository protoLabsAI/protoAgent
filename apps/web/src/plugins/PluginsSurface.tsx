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
import { useTrustAck } from "./TrustAckDialog";
import { PluginFreshness } from "./PluginFreshness";
import { usePluginManage, usePluginRefresh } from "./usePluginManage";
import { catalogCategories, filterCatalog } from "./catalog";
import {
  bundleLabel,
  distinctBundles,
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
  bundleName,
  description,
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
  /** bundle provenance label (ADR 0040) — set when a bundle installed this plugin */
  bundleName?: string | null;
  /** manifest description (#2248) — what the plugin does, under the name */
  description?: string;
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
      {/* Name · version · bundle chip · (only-when-actionable) freshness badge. */}
      <Td className="plugin-cell-name">
        <div className="plugin-row-head">
          <strong>{p.name}</strong>
          {p.version ? <span className="plugin-ver">v{p.version}</span> : null}
          {bundleName ? (
            <span className="plugin-chip" title={`Installed by the ${bundleName} bundle`}>
              {bundleName}
            </span>
          ) : null}
          <PluginFreshness update={update} />
        </div>
        {/* What it DOES (#2248) — the manifest already carried this and the row dropped it,
            so the display NAME had to smuggle it ("Coder (execution-grounded code-solve)").
            Clamped to two lines: a manifest can write a paragraph, and a row is not the
            place to read one — the full text is the title. */}
        {description ? (
          <p className="plugin-row-desc" title={description}>
            {description}
          </p>
        ) : null}
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
      // The shared plugin invalidation set (runtime + installed + freshness + settings
      // schema, #1423) — one refresh definition for every path that mutates plugin
      // state, console- or agent-initiated (ADR 0096 D8).
      refreshAll();
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
  // Consent gate (#2743): deps-install re-checks source trust server-side — a plugin
  // installed before a trust tightening answers needs_ack (nothing pip'd yet); the
  // shared hook asks, acks, and retries, same as the install flow.
  const { requestAck: requestDepsAck, ackDialog: depsAckDialog } = useTrustAck({
    onAckError: (m) => toast({ tone: "error", title: "Couldn't record the trust confirmation", message: m }),
  });
  const installDeps = useMutation({
    mutationFn: (p: Plugin) => api.installPluginDeps(p.id),
    onSuccess: (res, p) => {
      if (res.needs_ack) {
        requestDepsAck({ url: res.source ?? p.id, source: res.source ?? p.id, retry: () => installDeps.mutate(p) });
        return;
      }
      toast({
        tone: "success",
        title: "Dependencies installed",
        message: `${p.name}: ${(res.installed ?? []).join(", ") || "nothing to install"}.`,
      });
      refreshAll();
    },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't install deps", message: `${p.name}: ${errMsg(err)}` }),
  });

  // ── Bundle-level lifecycle (#2718, ADR 0049 D4) — the provenance chips' bundles,
  // now actionable: Update re-pins every member at the bundle's ref (retiring ones
  // the new manifest dropped); Uninstall removes exclusively-owned members + the
  // lock row (shared members stay). Distinct bundles derive from the inventory's
  // provenance join; freshness from the updates poll's `bundles` rows.
  // The lock registry is the truth when the backend sends it — a bundle whose
  // members were all removed individually has NO member rows but is still
  // installed (and uninstallable). Member-derivation stays as the older-backend
  // fallback only.
  const installedBundles =
    installed.data && "bundles" in installed.data
      ? (installed.data.bundles ?? []).filter((b) => b.id).map((b) => ({ id: b.id, name: b.name || b.id }))
      : distinctBundles(installed.data?.plugins);
  const bundleUpdateById = new Map((updates.data?.bundles ?? []).map((u) => [u.id, u]));
  const [bundleRemovePending, setBundleRemovePending] = useState<{ id: string; name: string } | null>(null);
  const updateBundle = useMutation({
    mutationFn: (b: { id: string; name: string }) => api.updateBundle(b.id),
    onSuccess: (res, b) => {
      refreshAll();
      // Read EVERY failure field the backend declares — a failed retirement or
      // enable-reload must never toast as full success while the member stays
      // live or disabled. Success only when nothing failed.
      const failed = Object.entries(res.load_errors ?? {});
      const retired = res.removed_members?.length ? ` Retired: ${res.removed_members.join(", ")}.` : "";
      const problems = [
        ...failed.map(([id, e]) => `${id} failed to load (${e})`),
        ...(res.enable_error ? [`enable-reload failed: ${res.enable_error}`] : []),
        ...(res.retire_error ? [`retire: ${res.retire_error}`] : []),
        ...(!res.reloaded && !res.enable_error ? ["not hot-reloaded — restart to apply"] : []),
      ];
      if (problems.length) {
        toast({
          tone: "error",
          title: "Bundle updated, with problems",
          message: `${b.name}: ${problems.join("; ")}.${retired}`,
        });
      } else {
        toast({
          tone: res.restart_recommended ? "info" : "success",
          title: "Bundle updated",
          message: `${b.name} re-pinned.${retired}${res.restart_recommended ? " Restart to serve the fresh routes." : ""}`,
        });
      }
    },
    onError: (err: unknown, b) => toast({ tone: "error", title: "Couldn't update bundle", message: `${b.name}: ${errMsg(err)}` }),
  });
  const removeBundle = useMutation({
    mutationFn: (b: { id: string; name: string }) => api.uninstallBundle(b.id),
    onSuccess: (res, b) => {
      refreshAll();
      const kept = res.kept?.length ? ` Kept (shared): ${res.kept.join(", ")}.` : "";
      if (res.reload_error) {
        toast({
          tone: "error",
          title: "Bundle uninstalled, but the reload failed",
          message: `${b.name} — removed ${res.removed_members.join(", ") || "nothing"}; ${res.reload_error}. Restart to finish.${kept}`,
        });
      } else {
        toast({
          tone: "success",
          title: "Bundle uninstalled",
          message: `${b.name} — removed ${res.removed_members.join(", ") || "nothing"}.${kept}`,
        });
      }
    },
    onError: (err: unknown, b) => toast({ tone: "error", title: "Couldn't uninstall bundle", message: `${b.name}: ${errMsg(err)}` }),
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
  const installedById = new Map((installed.data?.plugins ?? []).map((e) => [e.id, e]));
  const depsById = new Map((installed.data?.plugins ?? []).map((e) => [e.id, e.deps_missing ?? []]));
  const rows: InstalledRow[] = plugins.map((p) => ({
    p,
    behind: Boolean(updateById.get(p.id)?.behind),
    depsMissing: depsById.get(p.id) ?? [],
    // Bundle provenance (ADR 0040) — labels rows a bundle installed, so a stack's
    // members stop reading as anonymous individual plugins.
    bundle: installedById.get(p.id)?.bundle,
    // What the plugin DOES (#2248). The runtime status has no manifest, so the answer
    // only exists on the inventory side of this join — which is why the row used to
    // render a bare name and manifests smuggled their purpose into it.
    description: installedById.get(p.id)?.manifest?.description,
  }));
  const counts = statusCounts(rows);
  const shown = sortInstalled(filterInstalled(rows, q, status), sort);

  const renderRow = (row: InstalledRow) => (
    <PluginRow
      key={row.p.id}
      p={row.p}
      bundleName={bundleLabel(row)}
      description={row.description}
      update={updateById.get(row.p.id)}
      busy={pendingId === row.p.id}
      onToggle={onToggle}
      onUpdate={onUpdate}
      updating={updatingId === row.p.id}
      configurable={configurableIds.has(row.p.id)}
      removable={removableIds.has(row.p.id)}
      onRemove={onRemove}
      removing={removingId === row.p.id}
      depsMissing={row.depsMissing}
      onInstallDeps={(pl) => installDeps.mutate(pl)}
      installingDeps={installDeps.isPending && installDeps.variables?.id === row.p.id}
    />
  );

  return (
    <>
      {depsAckDialog}
      <PanelHeader
        title="Installed"
        kicker={`${counts.All} installed · ${counts.Loaded} loaded`}
        // Install-from-URL lives in the header (Josh, 2026-08-10): at the tail of the
        // search/filter toolbar it read as a filter and was routinely missed.
        actions={
          <Button type="button" onClick={() => setInstallOpen(true)} title="Install a plugin from a git URL">
            <Download size={14} /> Install from URL
          </Button>
        }
      />
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
            </div>
            {installedBundles.length ? (
              <div className="plugin-bundles-strip" role="group" aria-label="Installed bundles">
                {installedBundles.map((b) => {
                  const u = bundleUpdateById.get(b.id);
                  return (
                    <div key={b.id} className="plugin-bundle-row">
                      <span className="plugin-bundle-name">{b.name}</span>
                      {u?.behind ? <Badge status="info">update available</Badge> : null}
                      {u?.error ? <Badge status="warning">check failed</Badge> : null}
                      <Button
                        variant="ghost"
                        size="sm"
                        loading={updateBundle.isPending && updateBundle.variables?.id === b.id}
                        onClick={() => updateBundle.mutate(b)}
                      >
                        <RefreshCw size={13} /> Update
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setBundleRemovePending(b)}>
                        <Trash2 size={13} /> Uninstall
                      </Button>
                    </div>
                  );
                })}
              </div>
            ) : null}
            <ConfirmDialog
              open={bundleRemovePending !== null}
              title="Uninstall bundle?"
              confirmLabel="Uninstall"
              destructive
              onConfirm={() => {
                if (bundleRemovePending) removeBundle.mutate(bundleRemovePending);
                setBundleRemovePending(null);
              }}
              onClose={() => setBundleRemovePending(null)}
            >
              {bundleRemovePending
                ? `Uninstall ${bundleRemovePending.name} and the plugins only it installed? Members shared with another bundle are kept.`
                : undefined}
            </ConfirmDialog>
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
                        {/* An empty Attention view is the HEALTHY state — say so, instead of
                            the generic no-match line reading like something's wrong. */}
                        {status === "Attention" && !q.trim()
                          ? "Nothing needs attention — no errors, unfinished setup, available updates, or missing deps."
                          : `No plugins match${q ? ` "${q}"` : ""}${status !== "All" ? ` in ${status}` : ""}.`}
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

  // Consent gate (#2721): one-click install of an untrusted source answers needs_ack
  // (nothing fetched) — this path previously had NO confirm at all. The shared hook
  // asks, acks, and retries.
  const { requestAck, ackDialog } = useTrustAck({
    onAckError: (m) => toast({ tone: "error", title: "Couldn't record the trust confirmation", message: m }),
  });
  const install = useMutation({
    mutationFn: (p: CatalogPlugin) => api.installPlugin(p.repo),
    onSuccess: (res, p) => {
      if (res.needs_ack) {
        requestAck({ url: p.repo, source: res.source ?? p.repo, retry: () => install.mutate(p) });
        return;
      }
      qc.invalidateQueries({ queryKey: ["plugin-catalog"] });
      // Full installed-set refresh — this path used to invalidate only the catalog +
      // runtime, so the (5-min-stale) settings schema kept no group for the new plugin
      // and its Configure dialog opened EMPTY until a page refresh (#1643). It also
      // hid the row's Configure/Uninstall buttons (inventory + schema drive both).
      refreshAll();
      // "reloaded" alone isn't "live": the loader skips a plugin whose import fails
      // and the reload still succeeds (#2716) — don't toast success for dead code.
      const failed = Object.entries(res.load_errors ?? {});
      if (failed.length) {
        toast({
          tone: "error",
          title: "Plugin installed but not running",
          message: `${p.name}: ${failed.map(([, e]) => e).join("; ")}`,
        });
      } else {
        toast({ tone: "success", title: "Plugin installed", message: `${p.name}${res.reloaded ? " — enabled and live" : ""}.` });
      }
    },
    onError: (err: unknown, p) => toast({ tone: "error", title: "Couldn't install plugin", message: `${p.name}: ${errMsg(err)}` }),
  });
  const installingRepo = install.isPending ? install.variables?.repo : undefined;

  const plugins = catalog.data?.plugins ?? [];
  const categories = catalogCategories(plugins);
  const shown = filterCatalog(plugins, q, cat);

  return (
    <>
      {ackDialog}
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
