import { QueryErrorResetBoundary, useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense, useState } from "react";
import { ExternalLink, Github, Loader2, Store } from "lucide-react";

import { PanelHeader } from "../app/PanelHeader";
import { runtimeStatusQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { StatusPill } from "../app/StatusPill";
import { PluginsSection } from "../settings/PluginsSection";
import { api } from "../lib/api";
import type { RuntimeStatus } from "../lib/types";

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

function PluginRow({ p, busy, onToggle }: { p: Plugin; busy: boolean; onToggle: (p: Plugin) => void }) {
  const on = p.enabled;
  return (
    <div className="subagent-row" key={p.id}>
      <div>
        <strong>
          {p.name}
          {p.version ? <span className="muted"> v{p.version}</span> : null}
        </strong>
        <span>{contributionsLabel(p)}</span>
      </div>
      <div className="plugin-row-actions">
        <StatusPill
          label={p.loaded ? "loaded" : p.error ? "error" : p.enabled ? "enabled" : "disabled"}
          tone={p.loaded ? "success" : p.error ? "error" : "muted"}
        />
        <button
          type="button"
          className="ghost-button"
          disabled={busy}
          onClick={() => onToggle(p)}
          title={on ? `Disable ${p.name}` : `Enable ${p.name}`}
        >
          {busy ? <Loader2 size={14} className="spin" /> : on ? "Disable" : "Enable"}
        </button>
      </div>
    </div>
  );
}

function PluginsBody() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  const qc = useQueryClient();
  const [hint, setHint] = useState<string | null>(null);
  const toggle = useMutation({
    mutationFn: (p: Plugin) => api.setPluginEnabled(p.id, !p.enabled),
    onSuccess: (res, p) => {
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      setHint(
        res.restart_recommended
          ? `${p.name} ${res.enabled ? "enabled" : "disabled"} — restart to load its console view or background surface.`
          : `${p.name} ${res.enabled ? "enabled" : "disabled"}.`,
      );
    },
    onError: (err: unknown, p) => setHint(`Couldn't toggle ${p.name}: ${err instanceof Error ? err.message : String(err)}`),
  });
  const onToggle = (p: Plugin) => { setHint(null); toggle.mutate(p); };
  const pendingId = toggle.isPending ? toggle.variables?.id : undefined;

  const plugins = runtime.plugins ?? [];
  const byName = (a: Plugin, b: Plugin) => a.name.localeCompare(b.name);
  // Installed, grouped by status: loaded first, then disabled — alpha within each.
  const loaded = plugins.filter((p) => p.loaded).sort(byName);
  const disabled = plugins.filter((p) => !p.loaded).sort(byName);

  return (
    <>
      <PanelHeader
        title="Plugins"
        kicker={`${plugins.length} installed · ${loaded.length} loaded`}
      />
      <div className="stage-body">
        {hint ? <p className="plugin-hint">{hint}</p> : null}
        {/* 1 — Installed (loaded → disabled, alphabetical) */}
        {plugins.length ? (
          <>
            {loaded.length ? (
              <>
                <p className="panel-kicker">Loaded <span className="muted">· {loaded.length}</span></p>
                <div className="subagent-list">{loaded.map((p) => <PluginRow key={p.id} p={p} busy={pendingId === p.id} onToggle={onToggle} />)}</div>
              </>
            ) : null}
            {disabled.length ? (
              <>
                <p className="panel-kicker">Disabled <span className="muted">· {disabled.length}</span></p>
                <div className="subagent-list">{disabled.map((p) => <PluginRow key={p.id} p={p} busy={pendingId === p.id} onToggle={onToggle} />)}</div>
              </>
            ) : null}
          </>
        ) : (
          <div className="table-list">
            <div className="table-row">
              <span>no plugins installed — browse the marketplace or install from a git URL below</span>
              <StatusPill label="none" tone="muted" />
            </div>
          </div>
        )}

        {/* 2 — Marketplace (discover) */}
        <p className="panel-kicker">Marketplace</p>
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

        {/* 3 — Install (the PluginsSection ships its own "Install from a git URL" header) */}
        <PluginsSection />
      </div>
    </>
  );
}

export function PluginsSurface() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="plugins" />}>
            <Suspense fallback={<PanelSkeleton label="Loading plugins…" />}>
              <PluginsBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
