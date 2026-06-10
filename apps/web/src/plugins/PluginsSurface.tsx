import "../settings/plugins.css";

import { Button } from "@protolabsai/ui/primitives";
import { QueryErrorResetBoundary, useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";

import { Suspense, useState } from "react";
import { ExternalLink, Github, Loader2, RefreshCw, Store } from "lucide-react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { pluginUpdatesQuery, queryKeys, runtimeStatusQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { StatusPill } from "../app/StatusPill";
import { PluginsSection } from "../settings/PluginsSection";
import { PluginFreshness } from "./PluginFreshness";
import { api } from "../lib/api";
import type { PluginUpdate, RuntimeStatus } from "../lib/types";

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
}: {
  p: Plugin;
  update?: PluginUpdate;
  busy: boolean;
  onToggle: (p: Plugin) => void;
  onUpdate: (p: Plugin) => void;
  updating: boolean;
}) {
  const on = p.enabled;
  return (
    <div className="subagent-row" key={p.id}>
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
    onError: (err: unknown, p) => setHint(`Couldn't toggle ${p.name}: ${err instanceof Error ? err.message : String(err)}`),
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
    onError: (err: unknown, p) => setHint(`Couldn't update ${p.name}: ${err instanceof Error ? err.message : String(err)}`),
  });
  const onUpdate = (p: Plugin) => { setHint(null); update.mutate(p); };
  const updatingId = update.isPending ? update.variables?.id : undefined;
  const updateById = new Map((updates.data?.plugins ?? []).map((u) => [u.id, u]));

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
                <div className="subagent-list">{loaded.map((p) => <PluginRow key={p.id} p={p} update={updateById.get(p.id)} busy={pendingId === p.id} onToggle={onToggle} onUpdate={onUpdate} updating={updatingId === p.id} />)}</div>
              </>
            ) : null}
            {disabled.length ? (
              <>
                <p className="panel-kicker">Disabled <span className="muted">· {disabled.length}</span></p>
                <div className="subagent-list">{disabled.map((p) => <PluginRow key={p.id} p={p} update={updateById.get(p.id)} busy={pendingId === p.id} onToggle={onToggle} onUpdate={onUpdate} updating={updatingId === p.id} />)}</div>
              </>
            ) : null}
          </>
        ) : (
          <div className="table-list">
            <div className="table-row">
              <span>no plugins installed — see the Market or Download tabs</span>
              <StatusPill label="none" tone="muted" />
            </div>
          </div>
        )}
      </div>
    </>
  );
}

// Market — discover plugins (directory + GitHub topic).
function MarketTab() {
  return (
    <>
      <PanelHeader title="Market" kicker="discover plugins" />
      <div className="stage-body">
        <div className="plugin-market">
          <a className="plugin-market-link" href={DIRECTORY_URL} target="_blank" rel="noopener noreferrer">
            <Store size={16} />
            <span><strong>Browse the directory</strong><span className="muted">Curated + community plugins, with install URLs</span></span>
            <ExternalLink size={14} />
          </a>
          <a className="plugin-market-link" href={TOPIC_URL} target="_blank" rel="noopener noreferrer">
            <Github size={16} />
            <span><strong>GitHub topic</strong><span className="muted">Every repo tagged <code>protoagent-plugin</code></span></span>
            <ExternalLink size={14} />
          </a>
        </div>
        <p className="muted" style={{ marginTop: 10 }}>
          Found one? Copy its git URL and install it from the <strong>Download</strong> tab.
        </p>
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
  market: MarketTab,
  download: DownloadTab,
};

export function PluginsSurface({ tab = "local" }: { tab?: PluginsTab }) {
  const Body = TABS[tab] ?? LocalTab;
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="plugins" />}>
            <Suspense fallback={<PanelSkeleton label="Loading plugins…" />}>
              <Body />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
