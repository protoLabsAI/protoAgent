import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Plus, Square, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@protolabsai/ui/primitives";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { PanelHeader } from "@protolabsai/ui/navigation";

import { api, currentSlug } from "../lib/api";
import { fleetQuery, queryKeys } from "../lib/queries";
import type { FleetAgent } from "../lib/types";

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
                  {/* The host serves this console — it can't stop or remove itself. */}
                  {a.host ? null : (
                    <div className="fleet-row-actions">
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
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
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
