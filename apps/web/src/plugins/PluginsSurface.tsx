import "../settings/plugins.css";

import { Button } from "@protolabsai/ui/primitives";
import { useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

import { Suspense, useState } from "react";
import { Download, ExternalLink, Github, Loader2, RefreshCw, Search, Settings2, Store } from "lucide-react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { pluginUpdatesQuery, queryKeys, runtimeStatusQuery, settingsSchemaQuery } from "../lib/queries";
import { StagePanel } from "../app/ErrorBoundary";
import { errMsg } from "../lib/format";
import { StatusPill } from "../app/StatusPill";
import { PluginsSection } from "../settings/PluginsSection";
import { SettingsCategory } from "../settings/SettingsCategory";
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
}: {
  p: Plugin;
  update?: PluginUpdate;
  busy: boolean;
  onToggle: (p: Plugin) => void;
  onUpdate: (p: Plugin) => void;
  updating: boolean;
  configurable: boolean;
}) {
  const on = p.enabled;
  const [open, setOpen] = useState(false);
  return (
    <div className="plugin-row-wrap">
      <div className="subagent-row">
        <div>
          <strong>
            {p.name}
            {p.version ? <span className="muted"> v{p.version}</span> : null}
            <PluginFreshness update={update} />
          </strong>
          <span>{contributionsLabel(p)}</span>
        </div>
        <div className="plugin-row-actions">
          <StatusPill
            label={p.loaded ? "loaded" : p.error ? "error" : p.enabled ? "enabled" : "disabled"}
            tone={p.loaded ? "success" : p.error ? "error" : "muted"}
          />
          {update?.behind ? (
            <Button
              type="button"
              variant="ghost"
              disabled={updating}
              onClick={() => onUpdate(p)}
              title={`Update ${p.name} to the latest commit`}
            >
              {updating ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Update
            </Button>
          ) : null}
          {/* Config folded in (ADR 0059, bd-23a.3) — expand to edit this plugin's settings inline. */}
          {configurable ? (
            <Button
              type="button"
              variant="ghost"
              aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
              title={`Configure ${p.name}`}
            >
              <Settings2 size={14} /> Configure
            </Button>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            onClick={() => onToggle(p)}
            title={on ? `Disable ${p.name}` : `Enable ${p.name}`}
          >
            {busy ? <Loader2 size={14} className="spin" /> : on ? "Disable" : "Enable"}
          </Button>
        </div>
      </div>
      {configurable && open ? (
        <div className="plugin-row-config">
          <Suspense fallback={<p className="muted">Loading settings…</p>}>
            <SettingsCategory category="Plugins" pluginId={p.id} title={`${p.name} settings`} />
          </Suspense>
        </div>
      ) : null}
    </div>
  );
}

type PluginsTab = "local" | "market" | "download";

// Local — installed plugins, grouped Loaded → Disabled (alpha), with enable/disable.
function LocalTab() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  // Update status (ADR 0027) — joined per plugin id; degrades gracefully (non-suspense,
  // retry:false) so a missing updates API never blanks the Local tab.
  const updates = useQuery(pluginUpdatesQuery());
  const qc = useQueryClient();
  const [hint, setHint] = useState<string | null>(null);
  const toggle = useMutation({
    mutationFn: (p: Plugin) => api.setPluginEnabled(p.id, !p.enabled),
    onSuccess: (res, p) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      // Enable is fully live now (its router — which serves any console view — hot-mounts
      // on reload, #822). Only DISABLE leaves a stale route/surface behind (FastAPI can't
      // unmount), so restart_recommended is set only when turning a view/route plugin OFF.
      setHint(
        res.restart_recommended
          ? `${p.name} disabled — restart to fully remove its console view or background surface.`
          : `${p.name} ${res.enabled ? "enabled" : "disabled"}.`,
      );
    },
    onError: (err: unknown, p) => setHint(`Couldn't toggle ${p.name}: ${errMsg(err)}`),
  });
  const onToggle = (p: Plugin) => { setHint(null); toggle.mutate(p); };
  const pendingId = toggle.isPending ? toggle.variables?.id : undefined;

  const update = useMutation({
    mutationFn: (p: Plugin) => api.updatePlugin(p.id),
    onSuccess: (res, p) => {
      // Re-read BOTH the runtime plugin roster (new version/sha + reload state) and the
      // freshness probe (badge flips to "up to date").
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      qc.invalidateQueries({ queryKey: queryKeys.pluginUpdates });
      // Same restart-hint contract the enable flow uses: a view/route plugin can't swap
      // its mounted router live, so updating it recommends a restart.
      setHint(
        res.restart_recommended
          ? `${p.name} updated${res.version ? ` to v${res.version}` : ""} — restart to fully load its console view or background surface.`
          : `${p.name} updated${res.version ? ` to v${res.version}` : ""}${res.reloaded ? " (hot-reloaded)" : ""}.`,
      );
    },
    onError: (err: unknown, p) => setHint(`Couldn't update ${p.name}: ${errMsg(err)}`),
  });
  const onUpdate = (p: Plugin) => { setHint(null); update.mutate(p); };
  const updatingId = update.isPending ? update.variables?.id : undefined;
  const updateById = new Map((updates.data?.plugins ?? []).map((u) => [u.id, u]));

  // Which plugins have settings to fold in (ADR 0059) — the schema's plugin-tagged
  // groups. Non-suspense + cached, so it never blocks the list and dedupes with the
  // SettingsCategory the Configure expander renders.
  const schema = useQuery(settingsSchemaQuery());
  const configurableIds = new Set(
    (schema.data?.groups ?? []).filter((g) => g.plugin_id).map((g) => g.plugin_id as string),
  );

  const plugins = runtime.plugins ?? [];
  const byName = (a: Plugin, b: Plugin) => a.name.localeCompare(b.name);
  const loaded = plugins.filter((p) => p.loaded).sort(byName);
  const disabled = plugins.filter((p) => !p.loaded).sort(byName);

  return (
    <>
      <PanelHeader title="Installed" kicker={`${plugins.length} installed · ${loaded.length} loaded`} />
      <div className="stage-body">
        {hint ? <p className="plugin-hint">{hint}</p> : null}
        {plugins.length ? (
          <>
            {loaded.length ? (
              <>
                <p className="panel-kicker">Loaded <span className="muted">· {loaded.length}</span></p>
                <div className="subagent-list">{loaded.map((p) => <PluginRow key={p.id} p={p} update={updateById.get(p.id)} busy={pendingId === p.id} onToggle={onToggle} onUpdate={onUpdate} updating={updatingId === p.id} configurable={configurableIds.has(p.id)} />)}</div>
              </>
            ) : null}
            {disabled.length ? (
              <>
                <p className="panel-kicker">Disabled <span className="muted">· {disabled.length}</span></p>
                <div className="subagent-list">{disabled.map((p) => <PluginRow key={p.id} p={p} update={updateById.get(p.id)} busy={pendingId === p.id} onToggle={onToggle} onUpdate={onUpdate} updating={updatingId === p.id} configurable={configurableIds.has(p.id)} />)}</div>
              </>
            ) : null}
          </>
        ) : (
          <div className="table-list">
            <div className="table-row">
              <span>no plugins installed — see the Discover or Download tabs</span>
              <StatusPill label="none" tone="muted" />
            </div>
          </div>
        )}
      </div>
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
  const [hint, setHint] = useState<string | null>(null);

  const install = useMutation({
    mutationFn: (p: CatalogPlugin) => api.installPlugin(p.repo),
    onSuccess: (res, p) => {
      qc.invalidateQueries({ queryKey: ["plugin-catalog"] });
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      setHint(`${p.name} installed${res.reloaded ? " + enabled" : ""}.`);
    },
    onError: (err: unknown, p) => setHint(`Couldn't install ${p.name}: ${errMsg(err)}`),
  });
  const installingRepo = install.isPending ? install.variables?.repo : undefined;

  const plugins = catalog.data?.plugins ?? [];
  const categories = catalogCategories(plugins);
  const shown = filterCatalog(plugins, q, cat);

  return (
    <>
      <PanelHeader title="Discover" kicker={`${plugins.length} official plugins`} />
      <div className="stage-body">
        {hint ? <p className="plugin-hint">{hint}</p> : null}
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
                  <Button type="button" disabled={install.isPending} onClick={() => { setHint(null); install.mutate(p); }}>
                    {installingRepo === p.repo ? <Loader2 size={14} className="spin" /> : <Download size={14} />} Install
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

// Download — install from a git URL (PluginsSection ships its own header + form).
function DownloadTab() {
  return (
    <>
      <PanelHeader title="Download" kicker="install from a git URL" />
      <div className="stage-body">
        <PluginsSection />
      </div>
    </>
  );
}

const TABS: Record<PluginsTab, () => JSX.Element> = {
  local: LocalTab,
  market: DiscoverTab,
  download: DownloadTab,
};

export function PluginsSurface({ tab = "local" }: { tab?: PluginsTab }) {
  const Body = TABS[tab] ?? LocalTab;
  return (
    <StagePanel label="plugins">
      <Body />
    </StagePanel>
  );
}
