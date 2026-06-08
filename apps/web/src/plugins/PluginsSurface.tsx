import { QueryErrorResetBoundary, useSuspenseQuery } from "@tanstack/react-query";
import { Suspense } from "react";
import { ExternalLink } from "lucide-react";

import { PanelHeader } from "../app/PanelHeader";
import { runtimeStatusQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { StatusPill } from "../app/StatusPill";
import { PluginsSection } from "../settings/PluginsSection";

// Plugins: the installed plugins home — what's loaded, what each adds, and what
// errored. Browse + install more from the public directory.

function PluginsBody() {
  const { data: runtime } = useSuspenseQuery(runtimeStatusQuery());
  const plugins = runtime.plugins ?? [];
  const loaded = plugins.filter((p) => p.loaded).length;

  return (
    <>
      <PanelHeader
        title="Plugins"
        kicker={`${plugins.length} installed · ${loaded} loaded`}
        actions={
          <a
            className="icon-button"
            href="https://agent.protolabs.studio/plugins"
            target="_blank"
            rel="noopener noreferrer"
            title="Browse the plugin directory"
          >
            <ExternalLink size={16} />
          </a>
        }
      />
      <div className="stage-body">
        {plugins.length ? (
          <div className="subagent-list">
            {plugins.map((plugin) => (
              <div className="subagent-row" key={plugin.id}>
                <div>
                  <strong>
                    {plugin.name}
                    {plugin.version ? <span className="muted"> v{plugin.version}</span> : null}
                  </strong>
                  <span>
                    {[
                      plugin.loaded && plugin.tools.length ? `${plugin.tools.length} tool${plugin.tools.length === 1 ? "" : "s"}` : null,
                      plugin.loaded && plugin.skills ? `${plugin.skills} skill${plugin.skills === 1 ? "" : "s"}` : null,
                      plugin.views?.length ? `${plugin.views.length} view${plugin.views.length === 1 ? "" : "s"}` : null,
                      plugin.error || null,
                    ].filter(Boolean).join(" · ") || "no contributions"}
                  </span>
                </div>
                <StatusPill
                  label={plugin.loaded ? "loaded" : plugin.error ? "error" : plugin.enabled ? "enabled" : "disabled"}
                  tone={plugin.loaded ? "success" : plugin.error ? "error" : "muted"}
                />
              </div>
            ))}
          </div>
        ) : (
          <div className="table-list">
            <div className="table-row">
              <span>no plugins enabled — install one below, then add it to <code>plugins.enabled</code></span>
              <StatusPill label="none" tone="muted" />
            </div>
          </div>
        )}

        {/* Install / manage from a git URL (ADR 0027) — the management half. */}
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
