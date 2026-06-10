import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2, Play, Plus, Radar, Square, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@protolabsai/ui/primitives";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { PanelHeader } from "@protolabsai/ui/navigation";

import { api, currentSlug } from "../lib/api";
import { fleetQuery, queryKeys } from "../lib/queries";
import type { DiscoveredAgent, FleetAgent } from "../lib/types";

// Fleet manager (ADR 0042) — Settings → Agents. Lists the workspace agents with live
// status (the query polls every 3s, so a crashed agent flips to stopped on its own) and
// per-row start / stop / remove. "+ New agent" opens the archetype picker via `onNew`.
export function FleetManagerPanel({ onNew }: { onNew?: () => void }) {
  const qc = useQueryClient();
  const fleet = useQuery(fleetQuery());
  const [busy, setBusy] = useState<string | null>(null); // name currently being acted on
  const [error, setError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState<FleetAgent | null>(null);
  const [purge, setPurge] = useState(false);

  const agents = fleet.data?.agents ?? [];
  const slug = currentSlug(); // the agent this window is focused on (the URL slug)

  const run = useMutation({
    mutationFn: async (fn: () => Promise<unknown>) => fn(),
    onMutate: () => setError(null),
    onError: (e: Error) => setError(e.message),
    onSettled: () => {
      setBusy(null);
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
    },
  });
  const act = (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    run.mutate(fn);
  };

  // Agent-to-agent flows (ADR 0042 + 0025): add another agent as a `delegate_to` target of the
  // FOCUSED agent (this window's slug), pointing at its A2A endpoint — then this agent can
  // delegate work to it. The delegates query/POST is slug-scoped, so it lands on the focused agent.
  const delegatesQ = useQuery({ queryKey: ["delegates"], queryFn: () => api.delegates(), retry: false });
  const delegateNames = new Set((delegatesQ.data?.delegates ?? []).map((d) => d.name));
  // When an add 404s (the focused agent doesn't serve /api/delegates), we keep the attempted
  // entry so "Enable delegates" can retry it after enabling the plugin. Fleet agents ship with
  // delegates enabled at create (ADR 0042); the HOST carries no plugins by default — enabling
  // appends to plugins.enabled via applyConfig, and the reload hot-mounts the routes (#797),
  // so the retry succeeds without a restart.
  const [needsEnable, setNeedsEnable] = useState<{ name: string; url: string } | null>(null);
  const addDelegate = useMutation({
    mutationFn: (entry: { name: string; url: string }) =>
      api.createDelegate({ name: entry.name, type: "a2a", url: entry.url }),
    onMutate: () => {
      setError(null);
      setNeedsEnable(null);
    },
    onError: (e: Error, entry) => {
      if (/404|not found/i.test(e.message)) {
        setNeedsEnable(entry);
        setError("This agent can't hold delegates yet — the delegates plugin isn't enabled on it.");
      } else {
        setError(e.message);
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["delegates"] });
      void delegatesQ.refetch();
    },
  });
  const enableDelegates = useMutation({
    mutationFn: async (entry: { name: string; url: string }) => {
      const { config } = await api.config(); // the FOCUSED agent's live config (slug-scoped)
      const enabled = config.plugins?.enabled ?? [];
      if (!enabled.includes("delegates")) {
        await api.applyConfig({ plugins: { enabled: [...enabled, "delegates"] } }, null);
      }
      return entry;
    },
    onMutate: () => setError(null),
    onSuccess: (entry) => {
      setNeedsEnable(null);
      addDelegate.mutate(entry); // routes are hot-mounted on the reload — retry the add now
    },
    onError: (e: Error) => setError(e.message),
  });

  // Network discovery (ADR 0042 §I) — scan the box + LAN for OTHER protoAgents (not in this
  // fleet), then add a found one as a delegate of the focused agent (its A2A = url + /a2a).
  const [scanning, setScanning] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredAgent[] | null>(null);
  const scan = async () => {
    setScanning(true);
    setError(null);
    try {
      setDiscovered((await api.discoverAgents()).discovered);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setScanning(false);
    }
  };
  // Remote adds funnel through the same mutation, so a host-window 404 gets the
  // same enable-and-retry path as fleet-row adds.
  const addRemote = (d: DiscoveredAgent) => addDelegate.mutate({ name: d.name, url: `${d.url}/a2a` });

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title="Agents"
        kicker={`${agents.length} agent${agents.length === 1 ? "" : "s"} on this host · the fleet`}
        actions={
          <Button variant="primary" onClick={onNew}>
            <Plus size={15} /> New agent
          </Button>
        }
      />
      <div className="stage-body">
        {error ? (
          <p className="fleet-error" role="alert">
            {error}
            {needsEnable ? (
              <Button
                variant="default"
                disabled={enableDelegates.isPending || addDelegate.isPending}
                onClick={() => enableDelegates.mutate(needsEnable)}
                data-testid="enable-delegates">
                {enableDelegates.isPending ? "Enabling…" : "Enable delegates on this agent"}
              </Button>
            ) : null}
          </p>
        ) : null}
        {fleet.isLoading ? (
          <p className="fleet-empty">Loading the fleet…</p>
        ) : agents.length === 0 ? (
          <p className="fleet-empty">No agents yet — create one to get started.</p>
        ) : (
          <ul className="fleet-list">
            {agents.map((a) => {
              const isActive = (a.host ? "host" : a.id) === slug; // slug = stable id, not name
              return (
                <li key={a.name} className={`fleet-row${isActive ? " active" : ""}`}>
                  <span
                    className={`fleet-dot ${a.running ? "running" : "stopped"}`}
                    title={a.running ? "running" : "stopped"}
                    aria-label={a.running ? "running" : "stopped"}
                  />
                  <div className="fleet-row-main">
                    <span className="fleet-name">
                      {a.name}
                      {a.host ? <span className="fleet-host-tag">this instance</span> : null}
                      {isActive ? <span className="fleet-active-tag">active</span> : null}
                    </span>
                    <span className="fleet-meta">
                      :{a.port}
                      {a.pid ? ` · pid ${a.pid}` : ""}
                      {a.bundle ? ` · ${a.bundle}` : ""}
                    </span>
                  </div>
                  <div className="fleet-row-actions">
                    {/* Add as a delegate of the focused agent → enables delegate_to flows. Any
                        agent but the one you're on (it can't delegate to itself). */}
                    {!isActive ? (
                      delegateNames.has(a.name) ? (
                        <span className="fleet-delegate-tag" title="A delegate of this agent">delegate</span>
                      ) : (
                        <Button icon variant="ghost" title="Add as a delegate of this agent (delegate_to)"
                          disabled={addDelegate.isPending || !a.a2a}
                          onClick={() => addDelegate.mutate({ name: a.name, url: a.a2a! })}>
                          <Link2 size={14} />
                        </Button>
                      )
                    ) : null}
                    {/* The host serves this console — it can't stop or remove itself. */}
                    {a.host ? null : (
                      <>
                        {a.running ? (
                          <Button icon variant="ghost" title="Stop" disabled={busy === a.name}
                            onClick={() => act(a.name, () => api.stopAgent(a.name))}>
                            <Square size={14} />
                          </Button>
                        ) : (
                          <Button icon variant="ghost" title="Start" disabled={busy === a.name}
                            onClick={() => act(a.name, () => api.startAgent(a.name))}>
                            <Play size={14} />
                          </Button>
                        )}
                        <Button icon variant="ghost" title="Remove" disabled={busy === a.name}
                          onClick={() => { setPurge(false); setConfirmRemove(a); }}>
                          <Trash2 size={14} />
                        </Button>
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {/* Network discovery — scan the box + LAN for OTHER protoAgents and add one as a remote
            delegate of the focused agent (ADR 0042 §I). */}
        <div className="fleet-discover">
          <Button variant="ghost" onClick={scan} disabled={scanning}>
            <Radar size={14} /> {scanning ? "Scanning…" : "Discover agents on the network"}
          </Button>
          {discovered ? (
            discovered.length === 0 ? (
              <p className="fleet-empty">No other protoAgents found on the network.</p>
            ) : (
              <ul className="fleet-list">
                {discovered.map((d) => (
                  <li key={d.url} className="fleet-row">
                    <span className="fleet-dot running" aria-hidden />
                    <div className="fleet-row-main">
                      <span className="fleet-name">{d.name}</span>
                      <span className="fleet-meta">{d.url}</span>
                    </div>
                    <div className="fleet-row-actions">
                      {delegateNames.has(d.name) ? (
                        <span className="fleet-delegate-tag" title="A delegate of this agent">delegate</span>
                      ) : (
                        <Button icon variant="ghost" title="Add as a remote delegate (delegate_to)"
                          disabled={addDelegate.isPending}
                          onClick={() => addRemote(d)}>
                          <Link2 size={14} />
                        </Button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )
          ) : null}
        </div>
      </div>

      <ConfirmDialog
        open={confirmRemove !== null}
        title={confirmRemove ? `Remove ${confirmRemove.name}?` : ""}
        confirmLabel={purge ? "Remove + purge data" : "Remove"}
        destructive
        onConfirm={() => {
          const a = confirmRemove;
          const wipe = purge;
          setConfirmRemove(null);
          if (a) act(a.name, () => api.removeAgent(a.name, wipe));
        }}
        onClose={() => setConfirmRemove(null)}
      >
        <p>Stops the agent and removes it from the fleet.</p>
        <label className="fleet-purge">
          <input type="checkbox" checked={purge} onChange={(e) => setPurge(e.target.checked)} />
          Also purge its workspace data (irreversible)
        </label>
      </ConfirmDialog>
    </section>
  );
}
